"""
Sandy's permanent memory — everything Ruk says/does gets remembered
automatically. Powered by Mem0 (extracts facts, merges, resolves
contradictions) on top of Supabase pgvector.

ponytail: Mem0's own Supabase/pgvector integration handles the vector
store config — we don't hand-roll embeddings or similarity search.
"""
import os

from mem0 import Memory

from llm import log, CapExceeded, _check_and_bump_cap

RUK = "ruk"  # single user for now — multi-user is a config value away, not a rewrite

_MEM_PROVIDER = "gemini"
_MEM_FALLBACK_PROVIDER = "cerebras"
_config = {
    "vector_store": {
        "provider": "supabase",
        "config": {
            "connection_string": os.environ["SUPABASE_DB_CONNECTION_STRING"],
            "collection_name": "sandy_memories",
            "embedding_model_dims": 768,
        },
    },
    "llm": {
        "provider": "gemini",
        "config": {
            "model": "gemini-3.5-flash",
            "api_key": os.environ["GOOGLE_API_KEY"],
        },
    },
    "embedder": {
        "provider": "gemini",
        "config": {
            "api_key": os.environ["GOOGLE_API_KEY"],
            "embedding_dims": 768,
        },
    },
}

_fallback_config = {
    **_config,
    "llm": {
        "provider": "litellm",
        "config": {"model": "cerebras/gpt-oss-120b"},
    },
}

_memory: Memory | None = None
_memory_fallback: Memory | None = None


def _m() -> Memory:
    global _memory
    if _memory is None:
        _memory = Memory.from_config(_config)
    return _memory


def _m_fallback() -> Memory:
    global _memory_fallback
    if _memory_fallback is None:
        _memory_fallback = Memory.from_config(_fallback_config)
    return _memory_fallback


def remember(message: str, role: str = "user") -> None:
    """Store a message so Sandy can recall it later. Mem0 auto-extracts
    the actual facts worth keeping -- we don't decide what's important.

    Real 2-tier fallback, not a single point of failure: gemini first
    (cap-checked for real), and ONLY on gemini actually failing (its tiny
    20/day free-tier cap, or any other real error) does this fall back to
    Cerebras (also cap-checked, separately, for real) -- not Groq, which
    would silently reintroduce the exact contention bug that got Mem0
    moved off Groq in the first place. If BOTH fail, the raw message is
    still safe in chat_log -- losing one fact extraction is not worth
    crashing the background task over."""
    try:
        _check_and_bump_cap(_MEM_PROVIDER)
        _m().add(message, user_id=RUK, metadata={"role": role})
        return
    except CapExceeded as e:
        log(f"[memory.remember] {_MEM_PROVIDER} capped, trying fallback: {e}")
    except Exception as e:
        log(f"[memory.remember] {_MEM_PROVIDER} extraction failed ({e!r}), trying fallback")

    try:
        _check_and_bump_cap(_MEM_FALLBACK_PROVIDER)
        _m_fallback().add(message, user_id=RUK, metadata={"role": role})
    except CapExceeded as e:
        log(f"[memory.remember] fallback {_MEM_FALLBACK_PROVIDER} also capped, message stays in chat_log only: {e}")
    except Exception as e:
        log(f"[memory.remember] fallback {_MEM_FALLBACK_PROVIDER} also failed, message stays in chat_log only: {e!r}")


def recall(query: str, limit: int = 5) -> list[str]:
    """Pull the memories most relevant to the current message."""
    try:
        results = _m().search(query, filters={"user_id": RUK}, top_k=limit)
        return [r["memory"] for r in results["results"]]
    except Exception as e:
        log(f"[memory.recall] {_MEM_PROVIDER} search failed ({e!r}), trying fallback")

    try:
        results = _m_fallback().search(query, filters={"user_id": RUK}, top_k=limit)
        return [r["memory"] for r in results["results"]]
    except Exception as e:
        log(f"[memory.recall] fallback {_MEM_FALLBACK_PROVIDER} also failed, returning no memories this turn: {e!r}")
    return []


def get_all_facts(limit: int = 50) -> list[str]:
    """Every fact Mem0 has extracted and stored for Ruk, most recent
    first -- for Ruk's Home's Memory view. Separate from recall(), which
    is a semantic search against a specific query."""
    results = _m().get_all(filters={"user_id": RUK}, top_k=limit)
    return [r["memory"] for r in results["results"]]


if __name__ == "__main__":
    remember("Ruk's laptop is an Acer Ryzen 3 7320U with 8GB RAM")
    hits = recall("what laptop does Ruk have")
    assert any("Acer" in h or "Ryzen" in h for h in hits), f"memory didn't recall the fact: {hits}"
    print("memory.py: remember + recall OK ->", hits)
