-- Sandy rebuild -- Supabase schema
-- Run this once in the Supabase SQL editor (Project Settings -> SQL -> New query)
-- Day 1 + Day 2 tables only. Day 3+ tables (healing_ledger, projects, project_events,
-- device_sessions) come in their respective days.

-- ---------------------------------------------------------------
-- Day 1: identity, memory, config, observability, events, device
-- ---------------------------------------------------------------

-- Config: live-editable key/value (caps, preferences, etc).
create table if not exists sandy_config (
    key text primary key,
    value jsonb,
    updated_at timestamptz default now()
);

-- Chat log: verbatim message history.
create table if not exists chat_log (
    id uuid primary key default gen_random_uuid(),
    role text not null,           -- 'user' | 'assistant' | 'system'
    message text not null,
    session_id text,              -- optional grouping per conversation
    created_at timestamptz default now()
);
create index if not exists chat_log_created_idx on chat_log (created_at desc);
create index if not exists chat_log_session_idx on chat_log (session_id, created_at desc);

-- Daily usage per provider (for cap tracking, observability).
create table if not exists sandy_usage_daily (
    date date not null,
    provider text not null,
    calls int default 0,
    tokens_in bigint default 0,
    tokens_out bigint default 0,
    primary key (date, provider)
);

-- Mem0 long-term memory (pgvector). mem0ai creates tables named like
-- `mem0_memory`, `mem0_categories`, etc when it boots -- the actual
-- names are mem0's choice. See memory.py for how this is wired.

-- Orb graph event log: every node in the orb graph is one row here.
create table if not exists mastery_events (
    id bigint generated always as identity primary key,
    run_id text not null,
    agent text not null,                       -- 'sandy' | 'hermes'
    round int not null default 0,
    event_type text not null,                  -- planning|worker_call|verify|conflict|retry_similar|synthesis|obstacle|skill_saved|output
    provider text,
    summary text not null,
    detail text,
    parent_event_id bigint,
    related_event_ids bigint[],
    created_at timestamptz default now()
);
create index if not exists mastery_events_run_idx on mastery_events (run_id);
create index if not exists mastery_events_agent_idx on mastery_events (agent);

-- Device control: per-domain allowlist. Sandy MUST check this before
-- opening any URL.
create table if not exists device_permissions (
    domain text primary key,
    app_name text,
    granted boolean default false,
    granted_at timestamptz,
    last_used timestamptz
);

-- Device control: per-action audit log.
create table if not exists device_sessions (
    id uuid primary key default gen_random_uuid(),
    device_id text,
    app_name text,
    url text,
    started_at timestamptz default now(),
    ended_at timestamptz,
    actions jsonb default '[]'
);

-- Personality / learning: skill registry (Day 1).
create table if not exists skill_registry (
    skill text primary key,
    mastery_level int default 0,
    approaches jsonb default '[]',
    tools_effective jsonb default '[]',
    common_failures jsonb default '[]',
    time_invested_hours float default 0,
    updated_at timestamptz default now()
);

-- Personality / learning: awareness state (Day 1).
create table if not exists awareness_state (
    key text primary key,
    value jsonb,
    updated_at timestamptz default now()
);

-- Cross-device sync registry.
create table if not exists device_registry (
    device_id text primary key,
    name text,
    push_token text,
    last_seen timestamptz default now()
);

-- ---------------------------------------------------------------
-- Day 2: native mastery jobs (relational, not a jsonb blob)
-- ---------------------------------------------------------------

create table if not exists native_mastery_jobs (
    id text primary key,            -- run_id, primary key
    session_id text,
    skill text not null,
    mode text,                      -- 'continuous' | 'scheduled'
    weights jsonb default '{}'::jsonb,
    caps jsonb default '{}'::jsonb,
    usage jsonb default '{}'::jsonb,
    state text not null,            -- proposed|running|paused|scheduled_waiting|done|failed|removed
    plan text,
    result text,
    round int default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);
create index if not exists native_mastery_jobs_session_idx
    on native_mastery_jobs (session_id, state, created_at desc);
create index if not exists native_mastery_jobs_state_idx
    on native_mastery_jobs (state, created_at desc);
