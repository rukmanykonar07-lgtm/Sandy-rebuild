# Day 1 — Foundation & Core Infrastructure

Done:

- `requirements.txt` (FastAPI, litellm, mem0ai, playwright, etc.)
- `app/bootstrap.py` — single Supabase client (double-checked locking)
- `app/config.py` — live-editable config w/ in-process cache + atomic updates
- `app/llm.py` — 15-provider routing, circuit breaker, fallback, cap bumping
- `app/chatlog.py` — verbatim history, date-range extraction
- `app/memory.py` — Mem0 + Supabase pgvector, 2-tier fact-extract fallback
- `app/observability.py` — per-call token tracking, burn rate, cap status
- `app/identity.py` — Sandy's system prompt
- `app/personality.py` — mood state, awareness summary, skill registry (NEW)
- `app/events.py` — orb-graph event log
- `app/device/{__init__,browser,permissions}.py` — Playwright controller (NEW)
- `.gitignore`, `README.md`, `.env.example`

Next: Day 2 (brain orchestrator, native_mastery, mastery/Hermes).