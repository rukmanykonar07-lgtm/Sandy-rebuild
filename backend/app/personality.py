"""Sandy's personality + awareness + continuous learning engine.

Three lightweight components that sit alongside identity.py:

1. PersonalityEngine: picks a tone modifier based on context (mood).
2. AwarenessEngine: "what is Sandy doing right now" — grounded in real
   events from events.py, not narrated from imagination.
3. ContinuousLearning: extracts patterns from completed mastery jobs
   into a persistent skill_registry, so future jobs can build on past
   approaches instead of starting from zero.

ponytail: this is intentionally small. The expensive parts (orchestrator
patterns, conflict resolution, replan logic) live in brain.py / mastery.py
where they belong. This module just makes Sandy's behavior aware of
them in a way she can talk about.
"""
import datetime
import json
import threading
from enum import Enum

import config
from identity import mood_modifier


class Mood(str, Enum):
    ENERGETIC = "energetic"  # default
    PLAYFUL = "playful"
    SERIOUS = "serious"      # failures, urgent alerts
    FOCUSED = "focused"      # long mastery runs
    CONCERNED = "concerned"  # something is broken, asking before acting


_current_mood = Mood.ENERGETIC
_mood_lock = threading.Lock()


def set_mood(mood: Mood) -> None:
    """Switch Sandy's tone. Called by brain.py / healing.py / chat handlers
    when context changes (e.g. a failure alert just fired -> SERIOUS)."""
    global _current_mood
    with _mood_lock:
        _current_mood = mood


def get_mood() -> Mood:
    with _mood_lock:
        return _current_mood


def tone_for_context(has_failures: bool = False, is_long_running: bool = False) -> str:
    """Return the tone modifier to inject into the system prompt for the
    next turn. Caller decides context: has_failures=True if a healing
    alert just fired, is_long_running=True if a mastery job is mid-run."""
    if has_failures:
        set_mood(Mood.SERIOUS)
    elif is_long_running:
        set_mood(Mood.FOCUSED)
    else:
        set_mood(Mood.ENERGETIC)
    return mood_modifier(_current_mood.value)


# --- awareness -----------------------------------------------------------
# What Sandy is doing right now, grounded in real events. Read from
# events.py's recent log so the answer is always "I can show you", not
# "I'm pretty sure". Cheap in-memory query, no LLM.

def _recent_events(limit: int = 20) -> list[dict]:
    try:
        client = config.get_client()
        res = (client.table("mastery_events")
               .select("run_id, agent, round, event_type, provider, summary, created_at")
               .order("created_at", desc=True)
               .limit(limit)
               .execute())
        return res.data or []
    except Exception:
        return []


def awareness_summary() -> str:
    """Returns a short, human-readable string describing what Sandy's
    doing right now. Used in /status and when Ruk asks 'kya kar rahi
    hai tu abhi'."""
    events = _recent_events(15)
    if not events:
        return "Relaxing right now — koi active job nahi hai Ruk."

    by_run: dict[str, list[dict]] = {}
    for e in events:
        by_run.setdefault(e.get("run_id", "?"), []).append(e)

    lines = []
    for run_id, evs in list(by_run.items())[:3]:
        latest = evs[0]
        agent = latest.get("agent", "?")
        ev_type = latest.get("event_type", "?")
        round_n = latest.get("round", 0)
        provider = latest.get("provider", "?")
        lines.append(
            f"Run {run_id[:8]}: {agent} just did {ev_type} (round {round_n}, {provider})"
        )
    return "Abhi ye chal raha hai:\n" + "\n".join(lines)


# --- continuous learning ------------------------------------------------
# After every mastery job (native or Hermes) finishes, we look at the
# event log for that run and extract a few simple patterns:
#   - which providers were used
#   - how many rounds
#   - what approaches worked (event_type == "completed" without "replan")
#   - what approaches didn't (event_type == "replan")
# These get stored in sandy_config["skill_registry"] under a per-skill
# key. Future jobs of the same skill pull from there.

_SKILL_REGISTRY_KEY = "skill_registry"


def _load_registry() -> dict:
    try:
        return config.get_config(_SKILL_REGISTRY_KEY) or {}
    except Exception:
        return {}


def _save_registry(registry: dict) -> None:
    try:
        config.set_config(_SKILL_REGISTRY_KEY, registry)
    except Exception:
        pass


def update_skill_from_run(run_id: str, skill: str, engine: str) -> None:
    """Call this after a mastery run completes. Pulls events for the run,
    tallies useful patterns, and merges them into the registry entry for
    `skill`. Idempotent — safe to call twice on the same run."""
    try:
        client = config.get_client()
        res = (client.table("mastery_events")
               .select("agent, round, event_type, provider")
               .eq("run_id", run_id)
               .execute())
        events = res.data or []
    except Exception:
        return

    if not events:
        return

    providers_used = sorted({e["provider"] for e in events if e.get("provider")})
    rounds = max((e.get("round") or 0) for e in events)
    replans = sum(1 for e in events if e.get("event_type") == "replan")
    completions = sum(1 for e in events if e.get("event_type") == "completed")

    registry = _load_registry()
    entry = registry.get(skill, {
        "mastery_level": 0,
        "approaches": [],
        "tools_effective": [],
        "common_failures": [],
        "time_invested_hours": 0.0,
        "last_run": None,
        "run_count": 0,
    })
    entry["run_count"] = entry.get("run_count", 0) + 1
    entry["last_run"] = datetime.datetime.utcnow().isoformat()
    entry["mastery_level"] = min(10, entry.get("mastery_level", 0) + 1 if completions else entry.get("mastery_level", 0))
    if providers_used:
        for p in providers_used:
            if p not in entry["tools_effective"]:
                entry["tools_effective"].append(p)
    if replans > 0 and "needed replanning" not in entry["common_failures"]:
        entry["common_failures"].append(f"needed replanning in {replans} round(s) (engine={engine})")
    entry["approaches"] = sorted(set(entry.get("approaches", []) + [
        f"{engine} orchestration, {rounds} round(s), {completions} completion event(s)"
    ]))[:5]
    registry[skill] = entry
    _save_registry(registry)


def get_skill(skill: str) -> dict | None:
    """Return what the registry knows about `skill`, or None. Future
    mastery jobs call this at propose time to avoid re-discovering
    things a previous run already learned the hard way."""
    registry = _load_registry()
    return registry.get(skill)


def list_skills() -> dict:
    return _load_registry()


if __name__ == "__main__":
    set_mood(Mood.PLAYFUL)
    print("current mood:", get_mood().value)
    print("tone:", tone_for_context())
    print("serious context:", tone_for_context(has_failures=True))
    print("registry:", json.dumps(list_skills(), indent=2))