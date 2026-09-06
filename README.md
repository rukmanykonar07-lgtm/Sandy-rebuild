# Sandy (rebuild)

A from-scratch rewrite of Sandy's backend — same architecture (Supabase + FastAPI + PWA), new directory, new repo.

This is **Day 1** of a 1-week implementation plan. The foundation files are in place; the orchestrator, mastery engines, projects, healing, and UI are layered on top across Days 2–6.

See `PLAN.md` for the full week-by-week build plan.

## What's here now

```
backend/
  requirements.txt
  app/
    bootstrap.py         # single Supabase client construction
    config.py            # live-editable caps + atomic updates
    llm.py               # multi-provider LLM routing + circuit breaker
    chatlog.py           # verbatim history + date-range queries
    memory.py            # Mem0 + Supabase pgvector
    observability.py     # per-call token tracking + cap status
    identity.py          # Sandy's system prompt (Hinglish, "Ruk", never lies)
    personality.py       # NEW: mood state, awareness, skill registry
    events.py            # orb-graph event log
    device/              # NEW: Playwright browser automation
      __init__.py
      browser.py
      permissions.py
```

## What's coming (Days 2–6)

- **Day 2** — `brain.py` (orchestrator), `native_mastery.py`, `mastery.py` (Hermes)
- **Day 3** — `projects.py`, `healing.py`, `selfmod.py`, `notify.py`, `diagnostics.py`
- **Day 4** — `main.py` (FastAPI entry), `search.py`, `youtube.py`, `codebase.py`
- **Day 5** — frontend PWA (Home, Chat, Agents orb graphs, Workflows, Projects, Healing, Personality, Device Control)
- **Day 6** — tests, deploy to HF Spaces

## Running locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium   # for device control
cp .env.example .env          # fill in Supabase + provider keys
uvicorn app.main:app --reload --port 7860
```

Env vars expected (see `.env.example` for the full list):
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `SANDY_AUTH_KEY` (gates `/chat`)
- `HF_TOKEN` (for HF Spaces deploy)
- Provider keys: `GROQ_API_KEY`, `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, etc.