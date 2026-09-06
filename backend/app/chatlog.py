"""Raw, verbatim chat log — separate from memory.py's Mem0 fact-extraction.

Mem0 extracts facts from messages (great for Sandy recalling things about
Ruk), but it doesn't preserve exact message text in order. This does --
purely so Ruk's Home can show the real word-for-word conversation on
page refresh. Called automatically alongside memory.remember(), same as
memory itself: no one ever has to ask Sandy to log anything.
"""
import re
from datetime import date, datetime, timedelta, timezone

import config

from llm import log as _log

_N_DAYS_AGO_RE = re.compile(r"(\d+)\s*din\s*pehle|(\d+)\s*days?\s*ago", re.IGNORECASE)
_LAST_WEEK_RE = re.compile(r"\blast week\b|\bpichhle hafte\b|\bpichle hafte\b", re.IGNORECASE)
_THIS_WEEK_RE = re.compile(r"\bthis week\b|\bis hafte\b", re.IGNORECASE)
_RELATIVE_DAY_PATTERNS = [
    (r"\bday before yesterday\b|\bparso\b", 2),
    (r"\byesterday\b", 1),
]


def extract_date_range(message: str) -> tuple[str, str] | None:
    lower = message.lower()
    today = date.today()

    m = _N_DAYS_AGO_RE.search(lower)
    if m:
        n = int(m.group(1) or m.group(2))
        d = today - timedelta(days=n)
        return (d.isoformat(), (d + timedelta(days=1)).isoformat())

    if _LAST_WEEK_RE.search(lower):
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=7)
        return (start.isoformat(), end.isoformat())

    if _THIS_WEEK_RE.search(lower):
        start = today - timedelta(days=today.weekday())
        return (start.isoformat(), (today + timedelta(days=1)).isoformat())

    for pattern, days_back in _RELATIVE_DAY_PATTERNS:
        if re.search(pattern, lower):
            d = today - timedelta(days=days_back)
            return (d.isoformat(), (d + timedelta(days=1)).isoformat())

    return None

def _c():
    return config.get_client()


def log(message: str, role: str) -> None:
    try:
        _c().table("chat_log").insert({"role": role, "message": message}).execute()
    except Exception as e:
        _log(f"[chatlog] insert failed for {role} message: {e!r}")
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _c().table("chat_log").delete().lt("created_at", cutoff).execute()
    except Exception as e:
        _log(f"[chatlog] 30-day prune failed: {e!r}")


def get_history_in_range(start_date: str, end_date: str) -> list[dict]:
    try:
        res = (
            _c()
            .table("chat_log")
            .select("role, message, created_at")
            .gte("created_at", start_date)
            .lt("created_at", end_date)
            .order("created_at", desc=False)
            .execute()
        )
        return res.data
    except Exception:
        return []


def get_history(limit: int = 200) -> list[dict]:
    try:
        res = (
            _c()
            .table("chat_log")
            .select("role, message, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(res.data))
    except Exception:
        return []


if __name__ == "__main__":
    log("test message from chatlog.py self-check", role="user")
    hist = get_history(limit=5)
    assert any(h["message"] == "test message from chatlog.py self-check" for h in hist), (
        f"logged message didn't come back in history: {hist}"
    )
    print("chatlog.py: log + get_history OK ->", hist[-1])
