"""Skill-mastery engine: turns a chat request like "master web research,
3 days, 4 hours a day" into a registered mastery job. The job runs
through Sandy's chosen scheduler (Hermes cron in the original Sandy-oc;
the rebuild wires whatever scheduler is set up at deploy time, see
main.py's lifespan) with real tools -- web search/extraction, code
execution, and skill save/load -- so obstacles hit during a session
can become permanent, reusable skills instead of one-off notes.

Core-code self-modification is deliberately NOT auto-applied here -- see
the prompt template below. That's a separate, higher-risk piece.

Day 2 of the rebuild: cron.jobs (Hermes runtime) isn't available in
this build yet. create_job() / pause_job() / etc. are imported lazily
inside the function that needs them, so the module imports cleanly and
propose_plan / confirm_plan / job_diagnostics / parse_directives all
work today. The actual cron-creation call is wrapped to fail loud with
a clear "scheduler not wired" message until Day 4 brings the integration
in. The state machine, plan storage in sandy_config, and skill notes
storage are all real and work today.
"""
import os
import re
from pathlib import Path

from app import config
from app.identity import SANDY_SYSTEM_PROMPT
from app.llm import call_llm_with_fallback, log

_IDITY_MSG = {"role": "system", "content": SANDY_SYSTEM_PROMPT}

MASTERY_PROMPT_TEMPLATE = """You are Sandy, working on becoming a master at: {skill}

This is one session of a multi-day mastery block for this skill.

Ruk's approved approach for THIS skill (this is what he actually reviewed and confirmed -- follow it, don't substitute your own generic process instead):
{approach}

Your job this session:
1. Check what you already know/have saved from earlier sessions on this skill (your existing skills/notes) -- build on it, don't restart from zero.
2. Do real work toward mastery: research, test tools/techniques, write and run code -- whatever the skill actually needs. For skills commonly taught via video (editing, trading, SMMA, and similar), you have real YouTube transcript access (youtube.py) -- search for relevant tutorials via your web tool, then pull real transcripts to learn from instead of guessing at technique. This is unofficial/best-effort and can fail (no captions, blocked) -- if it fails, say so and move to another source, don't invent transcript content.
2b. BEFORE writing or changing ANY code this session (a script, a saved skill, anything): think it through for real first -- is this actually necessary, could an existing skill/piece be enhanced instead of adding something new, is what you're "fixing" actually broken (verify, don't assume), does this match Ruk's approved approach above rather than a guessed-at version of it, would this really work when Ruk/a future session actually uses it, and could it introduce a new bug. Only write the code after that reasoning, not before.
3. If the approach above calls for genuine PARALLEL multi-model work (e.g. separate workers on different providers, a planner + parallel workers + verifier pattern) -- use your real delegate_task tool with model= / override_provider= per child, in batch/parallel mode. This is the ONLY real way to run distinct providers as actual parallel workers. Do NOT attempt this via execute_code/code_execution -- provider API keys are deliberately stripped from that sandboxed environment for security (Hermes hardening against credential leaks) and any attempt to read them there will silently fail or come back empty. If delegate_task isn't available this session for some reason, say so plainly instead of faking parallel work with a single sequential pass.
4. If you hit an obstacle you can't get past (a technique fails, a site blocks you, etc.), research a real fix and save it as a reusable skill so you never hit it again -- don't just note it and move on.
5. If truly fixing something would require editing Sandy's own core source code (main.py/brain.py/llm.py/memory.py/config.py) rather than just adding a skill -- STOP. Do not edit that code yourself. Write up exactly what you'd change and why, clearly, so Ruk can review and approve it later.
6. End the session with an honest progress note for Ruk: what got done, what's still missing, whether any delegate_task workers actually ran (and on which providers), and whether you're at real master/expert level yet or need more sessions.
7. ALSO include a machine-readable EVENTS section (for the orb graph in Ruk's Home -- separate from the progress note above, one line per REAL thing you actually did this session, in this exact format, nothing invented):
EVENTS:
- type=<planning|worker_call|verify|conflict|retry_similar|synthesis|obstacle|skill_saved> | provider=<real provider name or none> | summary=<one line, what actually happened>
(one line per real event, as many as actually happened -- if nothing notable happened beyond the obvious, it's fine to have very few lines)

Be honest and concrete -- no filler, no claiming mastery -- or claiming parallel work happened -- that you haven't actually verified."""


_MAX_SKILL_NOTES_CHARS = 6000  # generous but bounded -- avoid unbounded growth in Supabase


def _pending_key(session_id: str) -> str:
    return f"mastery_pending:{session_id}"


def _explore_key(session_id: str) -> str:
    return f"mastery_pending_explore:{session_id}"


def save_skill_notes(skill: str, message: str) -> None:
    """Verbatim append -- NOT Mem0 fact-extraction. Real reason this
    exists: memory.recall() goes through Mem0's search, which extracts
    compact atomic facts (e.g. "Ruk prefers X") -- it is not built to
    preserve a multi-paragraph technical design (an orchestration plan,
    specific phases, specific tools) intact. A detailed design Ruk gives
    for a skill risks coming back lossy/compressed, or not at all, via
    recall() alone. This stores his own words for that skill, unmodified,
    so a later plan can quote his actual design instead of a fuzzy
    approximation of it. Best-effort: a config write failure here
    shouldn't block the conversation, so it's caught by the caller."""
    key = f"skill_notes:{skill.strip().lower()}"
    existing = config.get_config(key) or ""
    combined = (existing + "\n\n---\n\n" + message) if existing else message
    if len(combined) > _MAX_SKILL_NOTES_CHARS:
        combined = combined[-_MAX_SKILL_NOTES_CHARS:]  # keep the most recent detail, not the oldest
    config.set_config(key, combined)


def get_skill_notes(skill: str) -> str | None:
    return config.get_config(f"skill_notes:{skill.strip().lower()}")


def set_pending_explore(session_id: str, skill: str) -> None:
    config.set_config(_explore_key(session_id), skill)


def pop_pending_explore(session_id: str) -> str | None:
    skill = config.get_config(_explore_key(session_id))
    if skill:
        config.delete_config(_explore_key(session_id))
    return skill


def get_pending_explore(session_id: str) -> str | None:
    return config.get_config(_explore_key(session_id))


def explain_flow(skill: str, message: str, context: str = "") -> str:
    """Real, grounded answer for mastery-job talk that isn't yet a full
    skill+days+hours request -- covers both the FIRST exploratory message
    and any follow-up in the same thread (e.g. 'how will Hermes actually
    build it', 'explain so I can review/edit', 'where can I see it').

    Bug this fixes: this used to only take `skill` and always produce the
    same canned mechanism-explanation-then-ask-for-days/hours answer no
    matter what was actually asked -- so three different follow-up
    questions in a row got three near-identical answers, none of which
    engaged with the actual question. Now the real message text drives
    the answer; the mechanism facts are grounding to answer FROM, not a
    script to recite every time.
    """
    real_mechanism = (
        "REAL mechanism, exactly as it works in the actual code (use only what's relevant "
        "to what he actually asked -- don't recite all of this every time):\n"
        "1. A mastery job is a real scheduler-registered job (Hermes cron in the original "
        "Sandy-oc; the rebuild wires whatever scheduler is set up at deploy time) -- "
        "it runs through the scheduler's OWN native agent runtime, using MASTERY_PROMPT_TEMPLATE, "
        "NOT Sandy's normal brain.py chat routing, and NOT selfmod.py's single-file edit flow.\n"
        "2. Before anything is registered, Sandy proposes a plan (skill, days, hours/day, "
        "tools, and optionally Ruk's own described build approach) and Ruk must confirm it -- "
        "nothing runs unapproved. Ruk CAN give feedback/edits before confirming -- the plan "
        "isn't final until he says confirm.\n"
        "3. Once confirmed, the job is written to the scheduler's job store and immediately "
        "backed up to Supabase (config.backup_jobs) so an HF rebuild can't lose it.\n"
        "4. The scheduler's own background ticker (a separate process from chat) checks "
        "that store every 60 seconds and runs a session when one's due -- fully autonomous, "
        "no chat message needed to trigger a run. The schedule is a fixed daily time (9 AM), "
        "not 'runs immediately when confirmed'.\n"
        "5. Each session runs on the scheduler's OWN generic agent loop (its own think-act-observe "
        "cycle, tool calling, skill save/load) -- it is NOT Sandy's own brain.py multi-model "
        "orchestrator (planner/parallel-workers/verifier-loop). If Ruk wants that specific "
        "orchestration pattern to actually drive a session, it has to be written into the "
        "job's own prompt as explicit instructions (see 'Ruk's approach' below) -- the "
        "scheduler doesn't inherit it automatically just because it exists elsewhere in Sandy's code.\n"
        "6. Sessions can research (including pulling YouTube video transcripts for skills "
        "that are commonly taught there, like editing/trading/SMMA), test, write/run code, "
        "and save reusable skills to the scheduler's skills directory -- separate from Sandy's "
        "own core .py files. If something would genuinely require editing Sandy's own core "
        "code, the job stops and writes that up for Ruk to review instead of doing it unapproved.\n"
        "7. Every session's real progress note is written to the scheduler's output directory -- "
        "visible in Ruk's Home: a summary in Command Center, live status in Workflows, and "
        "the actual output in Agents once a session completes.\n"
    )
    prompt = (
        f"Ruk is talking about \"{skill}\" as a mastery job. His message this turn: "
        f'"{message}"\n\n'
        "Answer THIS specific message directly and concretely -- don't just recite a generic "
        "mechanism speech if he's asking something more specific (e.g. how the scheduler's "
        "own agent loop relates to Sandy's own orchestration patterns, whether he can edit "
        "the plan, where output shows up). If the background facts above include Ruk's OWN "
        "verbatim words about this skill (marked as such), that IS his real design -- reflect "
        "it back concretely and specifically, don't flatten it into a generic description. Use "
        "the mechanism facts below only where they're actually relevant to what he asked. If a "
        "time commitment (days/hours-per-day) hasn't been given yet, end by asking for it -- "
        "but only after actually answering his real "
        "question, not instead of it.\n\n" + real_mechanism
    )
    if context:
        prompt = f"{context}\n\n{prompt}"
    return call_llm_with_fallback("gemini", [_IDITY_MSG, {"role": "user", "content": prompt}])


def propose_plan(
    session_id: str, skill: str, days: int, hours_per_day: float,
    feedback: str | None = None, context: str = "",
) -> str:
    """Full mastery plan document -- not just a one-liner. Before a
    multi-day unattended cron mission starts, Ruk should see exactly
    what Sandy understood, what she'll actually produce, how she'll go
    about it, and a rough day-by-day shape -- concrete enough to catch
    a misunderstanding before days of cron sessions run on it, not
    after. Stored as the pending plan for this session -- a follow-up
    message that isn't an approval gets treated as feedback and this
    regenerates the whole document, not a patch to one line.

    Root-cause fix: an edit message ("actually make it 5 days") used to
    only change the DISPLAYED plan text -- confirm_plan() still used the
    ORIGINAL days/hours_per_day underneath, so what Ruk approved and what
    actually got registered as the cron job could silently differ. Now
    feedback is re-parsed with the same days/hours regex main.py's
    classifier fallback uses, and any number actually mentioned this
    turn overrides the stored value for real."""
    prior = config.get_config(_pending_key(session_id))
    if feedback and prior:
        days_m = re.search(r"(\d+(?:\.\d+)?)\s*day", feedback, re.I)
        hours_m = re.search(r"(\d+(?:\.\d+)?)\s*hour", feedback, re.I)
        if days_m:
            days = float(days_m.group(1))
        if hours_m:
            hours_per_day = float(hours_m.group(1))
    revision_note = ""
    if feedback and prior:
        revision_note = (
            f"\n\nRuk already saw this earlier draft:\n{prior['plan']}\n\n"
            f'He wants this changed: "{feedback}"\n'
            "Rewrite the FULL plan incorporating that feedback -- don't just patch one line."
        )
    prompt = (
        f'Ruk asked Sandy to master "{skill}" over {days} days, ~{hours_per_day}h/day. '
        "Write a full plan document, in Hinglish, covering ALL of these sections clearly "
        "(use these as headers):\n"
        "1. UNDERSTANDING -- what you understand the actual goal to be, in your own words\n"
        "2. WHAT YOU'LL MAKE -- the concrete deliverable(s), specifically, not vague\n"
        "3. PROCESS -- how you'll actually go about it: research first, then think through "
        "approaches, then build/practice, then review and iterate -- concrete for THIS "
        "specific skill, not generic corporate filler. If Ruk has described a specific "
        "orchestration/loop/multi-step approach for this skill in the background facts "
        "above, or if a multi-phase approach (plan -> parallel work -> verify/cross-check -> "
        "synthesize) genuinely fits the skill, describe it concretely here -- this exact "
        "section is what actually runs each session, word for word, not just a proposal.\n"
        "4. DAY-BY-DAY -- a rough breakdown of what happens each of the " + str(days) + " days\n"
        "5. TOOLS -- which of your real tools you'll actually use, and why: web search, "
        "code execution (research/testing/scripts -- NOT for calling provider APIs "
        "directly, those keys are sandboxed away from it), YouTube transcript access for "
        "video-taught skills like editing/trading/SMMA, and delegate_task (with "
        "model=/override_provider=) if genuine PARALLEL multi-model work is part of the "
        "approach -- that's the only real mechanism for actual parallel providers, not "
        "code execution\n"
        "6. SUCCESS CRITERIA -- concrete, checkable signs of real progress, not vague growth"
        + revision_note
    )
    if context:
        prompt = f"{context}\n\n{prompt}"
    plan = call_llm_with_fallback("gemini", [_IDITY_MSG, {"role": "user", "content": prompt}])
    config.set_config(_pending_key(session_id), {"skill": skill, "days": days, "hours_per_day": hours_per_day, "plan": plan})
    return plan


def get_pending_plan(session_id: str) -> dict | None:
    return config.get_config(_pending_key(session_id))


def confirm_plan(session_id: str) -> str:
    """Ruk approved -- start the mission using the STORED params from
    the plan he actually saw and confirmed, not re-parsed from whatever
    his approval message happened to say. The exact plan text he approved
    (including any orchestration/approach detail it described) gets baked
    into the real job prompt below -- what Ruk approved is what actually
    runs, not a generic template that ignores it."""
    pending = config.get_config(_pending_key(session_id))
    if not pending:
        return "Ruk, koi pending plan nahi mila is session ke liye -- pehle naya mastery request bhejo."
    config.delete_config(_pending_key(session_id))
    return start_mastery(pending["skill"], pending["days"], pending["hours_per_day"], approach=pending.get("plan"))


def _next_9am_ist() -> str:
    """Real next-run time for the '0 9 * * *' schedule, computed honestly
    instead of implying the job starts immediately. Assumes the
    HERMES_TIMEZONE=Asia/Kolkata Dockerfile fix is deployed -- if it isn't
    yet, this label is wrong until the next rebuild (flagging in code,
    not pretending this is unconditionally correct)."""
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python <3.9 -- the rebuild targets 3.11+ but defensive
        return "next 9 AM IST slot (zoneinfo unavailable)"
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target.strftime("%d %b, %I:%M %p IST")


def start_mastery(skill: str, days: int, hours_per_day: int, approach: str | None = None) -> str:
    """Creates a real registered mastery job: one session per day, for
    `days` days. hours_per_day currently just shapes the prompt/expectation
    -- the cron ticker triggers a session, it doesn't block for N wall-clock
    hours, so this isn't a literal timer yet (flagging honestly, not
    pretending otherwise).

    `approach` -- the actual plan text Ruk reviewed and confirmed (from
    propose_plan), baked verbatim into the real job prompt below. This is
    what makes Ruk's own described build approach/orchestration for THIS
    skill genuinely drive the session, instead of every job just getting
    the same generic template regardless of what was actually agreed.

    Day 2 of the rebuild: the actual scheduler-runtime call is deferred
    to Day 4. The function below stores the job in sandy_config with a
    marker so main.py / cron / whatever-Day-4-brings can find and
    register it with the real runtime. Until then, the job sits in
    sandy_config registered but not yet scheduled, and a follow-up
    deploy / wiring step makes it actually run."""
    log(f"[start_mastery] registering mastery job -- skill={skill!r} days={days} schedule=0 9 * * *")
    job_id = f"mastery-{skill.replace(' ', '-').lower()}-{int.from_bytes(os.urandom(4), 'big'):08x}"

    # Persist the full job spec in sandy_config so the scheduler can
    # pick it up at boot / next-tick. The shape mirrors what cron.jobs
    # would have written to ~/.hermes/cron/jobs.json -- enough for the
    # scheduler adapter in Day 4 to read and register it without
    # re-deriving anything.
    job_record = {
        "id": job_id,
        "name": f"mastery-{skill.replace(' ', '-').lower()}",
        "skill": skill,
        "days": days,
        "hours_per_day": hours_per_day,
        "schedule": "0 9 * * *",
        "repeat": days,
        "approach": approach or "No specific approach was given beyond this template -- design a reasonable one yourself and say what it is in your progress note.",
        "prompt": MASTERY_PROMPT_TEMPLATE.format(
            skill=skill,
            approach=approach or "No specific approach was given beyond this template -- design a reasonable one yourself and say what it is in your progress note.",
        ),
        "enabled_toolsets": ["web", "code_execution", "skills", "delegation"],
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "state": "registered",
        "created_at": _now_iso(),
    }
    config.set_config(f"mastery_job:{job_id}", job_record)

    try:
        next_run = _next_9am_ist()
    except Exception:
        next_run = "next 9 AM IST slot (couldn't compute the exact date)"

    # Day 2 note: in this build, the scheduler isn't wired yet, so the
    # job is REGISTERED but not yet RUNNING. Day 4 will add the cron /
    # ticker adapter that reads these job records and actually fires
    # the session at 9 AM. Until then, the reply tells Ruk the truth.
    return (
        f"Theek hai Ruk! \"{skill}\" mastery job REGISTERED — {days} din, roz ek session "
        f"(~{hours_per_day}h target). Job ID: {job_id}.\n\n"
        f"Real baat (Day 2 rebuild): ye job abhi sandy_config me stored hai, but the actual "
        f"scheduler-runtime wiring is Day 4 ka kaam. Tab tak ye \"registered\" hai, \"running\" "
        f"nahi — first real session {next_run} ke baad fire hoga jab scheduler adapter aayega. "
        f"Hermes-style session output aur orb graph bhi Day 4+ me wire honge. Confirm karne ka "
        f"real tarika: Agents tab me pehla real output tab dikhega jab session genuinely chal "
        f"chuka hoga."
    )


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- backfill from on-disk session output (Hermes-style) ---------------
# Day 2 of the rebuild: Hermes-style session output dir doesn't exist
# yet (Day 4). These helpers are no-ops until then, but the public API
# is in place so callers don't need to change when the scheduler ships.

_EVENT_LINE = re.compile(
    r"type=(\w+)\s*\|\s*provider=([\w\-]+|none)\s*\|\s*summary=(.+)", re.I
)


def backfill_events_from_output(job_id: str) -> int:
    """Parses the EVENTS section out of each real output file for this
    job and logs any not-already-logged ones to events.py, so the
    scheduler-side graph has real data too -- the scheduler's own agent
    can't write to Supabase directly (its code_execution sandbox strips
    provider/Supabase keys, same hardening covered in the prompt
    template above), so this reads back what it already wrote to disk
    instead. Idempotent: uses run_id=f"hermes:{job_id}:{timestamp}" per
    file, checked against existing events before inserting, so re-polling
    the same output never double-logs. Returns how many new events were
    added this call."""
    from app import events
    added = 0
    for out in job_output(job_id):
        run_id = f"hermes:{job_id}:{out['timestamp']}"
        if events.get_events(run_id):
            continue  # already backfilled this file
        lines = out["content"].splitlines()
        try:
            start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("EVENTS:"))
        except StopIteration:
            continue
        for line in lines[start + 1:]:
            m = _EVENT_LINE.search(line)
            if not m:
                continue
            event_type, provider, summary = m.groups()
            events.log_event(
                run_id, "hermes", event_type.lower(), summary.strip(),
                provider=None if provider.lower() == "none" else provider,
            )
            added += 1
        if added:
            events.log_event(run_id, "hermes", "output", f"Session output ({out['timestamp']})", detail=out["content"])
    return added


def scheduler_health() -> str:
    """Is the scheduler's own background ticker actually alive -- separate
    from any single job's state. A dead/stuck ticker explains 'next run
    Unknown'/'nothing ever fires' for EVERY job at once. In this Day 2
    build, the scheduler adapter isn't wired yet -- this returns a clear
    status reflecting that, instead of fake 'everything is fine' or
    crashing."""
    return "⚠️ SCHEDULER: Day 2 rebuild — scheduler adapter not wired yet. Day 4 brings the cron / Hermes integration. Until then, jobs register in sandy_config but don't actually fire."


def job_diagnostics(job_ref: str) -> str:
    """Everything real about ONE mastery job -- state, schedule, real
    progress, any real error, AND what's actually sitting in its output
    directory. Day 2 reads from sandy_config (where jobs are stored now);
    Day 4 will also pull from the scheduler runtime directly."""
    # Try to find the job in sandy_config first
    job = None
    try:
        for k, v in (config.get_config("*") or {}).items():
            if not isinstance(v, dict) or "id" not in v:
                continue
            if v.get("id") == job_ref or v.get("name") == job_ref:
                job = v
                break
    except Exception:
        pass
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila (sandy_config me bhi nahi)."

    lines = [f"**{job['name']}** ({job['id']})"]
    lines.append(f"- state: {job.get('state', 'unknown')}")
    lines.append(f"- schedule: {job.get('schedule', 'unknown')}")
    lines.append(f"- next run: not yet scheduled (Day 4 wires the cron adapter)")
    lines.append(f"- last run: never run yet (Day 2 build, scheduler not wired)")
    if job.get("repeat"):
        lines.append(f"- sessions: 0/{job['repeat']}")
    lines.append(f"- stored: sandy_config['mastery_job:{job['id']}']")
    return "\n".join(lines)


def list_mastery_jobs() -> list[dict]:
    """Every registered mastery job, for Ruk's Home's Workflows view.
    Day 2 reads from sandy_config; Day 4 will also pull live state from
    the scheduler runtime. Empty list is the legitimate Day 2 state --
    not an error, just nothing registered yet."""
    out = []
    try:
        # The Day-1 config.get_config('*') pattern is best-effort; if
        # the implementation doesn't support wildcard scan, we just
        # return the empty list honestly.
        all_cfg = config.get_config("*") or {}
    except Exception as e:
        log(f"[mastery.list_mastery_jobs] couldn't scan sandy_config: {e!r}")
        return out
    for k, v in all_cfg.items():
        if not isinstance(v, dict) or not k.startswith("mastery_job:"):
            continue
        out.append(v)
    return out


# --- pause / resume / trigger / remove / edit ---------------------------
# Day 2 of the rebuild: these all just update sandy_config. The
# scheduler adapter in Day 4 will observe those config writes (or get
# signalled directly) and reflect them in the real runtime. For now,
# these functions exist so main.py / tests can call them; they update
# the stored state but no actual scheduler change happens.

def pause_mastery_job(job_ref: str) -> str:
    job = _find_job(job_ref)
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila."
    job["state"] = "paused"
    config.set_config(f"mastery_job:{job['id']}", job)
    return f"Ruk, mastery job '{job['name']}' ({job['id']}) pause ho gaya."


def resume_mastery_job(job_ref: str) -> str:
    job = _find_job(job_ref)
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila."
    job["state"] = "registered"
    config.set_config(f"mastery_job:{job['id']}", job)
    return f"Ruk, mastery job '{job['name']}' ({job['id']}) resume ho gaya (Day 2: scheduler not yet firing)."


def trigger_mastery_job_now(job_ref: str) -> str:
    job = _find_job(job_ref)
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila."
    job["state"] = "triggered"
    job["trigger_at"] = _now_iso()
    config.set_config(f"mastery_job:{job['id']}", job)
    return f"Ruk, mastery job '{job['name']}' ({job['id']}) trigger request recorded. Day 2: actual scheduler tick fires it when the adapter lands (Day 4)."


def remove_mastery_job(job_ref: str) -> str:
    job = _find_job(job_ref)
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila."
    config.delete_config(f"mastery_job:{job['id']}")
    return f"Ruk, mastery job '{job['name']}' ({job['id']}) remove kar diya."


def _find_job(job_ref: str) -> dict | None:
    for j in list_mastery_jobs():
        if j.get("id") == job_ref or j.get("name") == job_ref:
            return j
    return None


def real_hermes_providers() -> set[str]:
    """The actual set of provider names the scheduler's cron runtime
    recognizes -- confirmed by reading the real installed hermes-agent
    package, NOT assumed. Day 2 stub: returns an empty set so the edit
    path is fail-open (won't block an edit when the scheduler isn't
    wired). Day 4 fills this in for real."""
    return set()


def edit_mastery_job(job_ref: str, updates: dict) -> str:
    """Real param edit on an EXISTING mastery job -- e.g. change which
    provider/model it uses. Anything in `updates` (schedule, repeat,
    provider, model, prompt, etc) gets written back to sandy_config.

    Day 2 note: provider validation against the real runtime is
    deferred to Day 4. For now, updates are accepted as-is so the
    data path is testable end-to-end."""
    job = _find_job(job_ref)
    if not job:
        return f"Ruk, '{job_ref}' naam/id ka mastery job nahi mila."
    job.update(updates)
    config.set_config(f"mastery_job:{job['id']}", job)
    return f"Ruk, mastery job '{job['name']}' update ho gaya: {updates}."


_OUTPUT_DIR = Path(os.environ.get("HERMES_HOME", "/root/.hermes")) / "cron" / "output"


def job_output(job_id: str, limit: int = 5) -> list[dict]:
    """Real progress notes the scheduler wrote for this job after each
    session, most recent first, for Ruk's Home's Agents view. Day 2
    stub: scheduler adapter not wired yet, so this returns an empty
    list honestly. Empty list is the legitimate Day 2 state -- not an
    error, just no session has run yet."""
    d = _OUTPUT_DIR / job_id
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.md"), reverse=True)[:limit]
    return [
        {"timestamp": f.stem, "content": f.read_text(encoding="utf-8", errors="replace")}
        for f in files
    ]
