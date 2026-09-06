"""Per-domain device permissions.

Stored in Supabase `device_permissions`. Sandy MUST check this before
opening any URL. The check is cheap and synchronous (single row lookup)
so it doesn't slow down the chat path.

First-time flow: when a user asks Sandy to open a new domain:
  1. This module's is_approved() returns False
  2. Sandy pauses, asks Ruk in chat ("Ruk, main mail.google.com pehli
     baar khol rahi hoon. Theek hai?")
  3. If Ruk says yes, main.py calls grant() to record the approval
  4. Next time, is_approved() returns True and the open goes through

No automatic re-approval. Once denied, stays denied. Ruk can call
revoke() in chat to remove an existing grant.
"""
import datetime

import config

_TABLE = "device_permissions"


def _c():
    return config.get_client()


def is_approved(domain: str) -> bool:
    """Cheap pre-flight check. Returns True if `domain` is in the
    allowlist, False otherwise. Sync call to Supabase -- one row, no
    joins, fast enough not to cache."""
    try:
        res = _c().table(_TABLE).select("granted").eq("domain", domain).execute()
        return bool(res.data and res.data[0].get("granted"))
    except Exception:
        return False


def grant(domain: str, app_name: str | None = None) -> dict:
    """Add or update a domain in the allowlist. Idempotent."""
    now = datetime.datetime.utcnow().isoformat()
    row = {
        "domain": domain,
        "app_name": app_name or domain,
        "granted": True,
        "granted_at": now,
        "last_used": now,
    }
    _c().table(_TABLE).upsert(row).execute()
    return row


def revoke(domain: str) -> None:
    """Remove a grant. Silent if it doesn't exist."""
    _c().table(_TABLE).delete().eq("domain", domain).execute()


def list_grants() -> list[dict]:
    """All current grants, for the device tab UI."""
    try:
        res = _c().table(_TABLE).select("*").order("granted_at", desc=True).execute()
        return res.data or []
    except Exception:
        return []


def touch(domain: str) -> None:
    """Update last_used timestamp. Best-effort, no error surfacing."""
    try:
        _c().table(_TABLE).update(
            {"last_used": datetime.datetime.utcnow().isoformat()}
        ).eq("domain", domain).execute()
    except Exception:
        pass
