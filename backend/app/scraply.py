"""scraply -- Sandy's scraping engine, wrapping Scrapling (D4Vinci/Scrapling).

Two modes:
  fast    -- plain HTTP via FetcherSession with TLS impersonation, no
             browser. Default for everything: cheap, quick, HF-free-tier
             friendly.
  stealth -- StealthyFetcher (real browser, solves Cloudflare). Opt-in
             only, gated behind sandy_config["scrapling_stealth"]=true,
             because the browser fetchers need Playwright/Camoufox
             downloads that don't belong on the free tier by default.

Contract (per plan Part 3):
  - returns a structured dict {ok, url, title, markdown, status} or
    {ok: False, url, error}; NEVER raises into chat/research paths.
  - size cap + markdown truncation before anything reaches a prompt,
    so token discipline holds even on a 1MB page dump.
  - if the scrapling import itself fails at runtime (bad rebuild), the
    module degrades to snippet-only callers by returning ok=False --
    logged once, non-fatal.
"""
from app import config
from app.llm import log

try:
    from scrapling.fetchers import FetcherSession, StealthyFetcher
    _IMPORT_OK = True
    _IMPORT_ERROR = None
except Exception as _import_err:  # pragma: no cover - depends on deploy env
    _IMPORT_OK = False
    _IMPORT_ERROR = _import_err

MAX_BYTES = 512 * 1024          # hard cap on raw page body we keep
MAX_MARKDOWN_CHARS = 12_000     # cap before the result enters any prompt
FETCH_TIMEOUT = 20              # seconds


def stealth_enabled() -> bool:
    """Stealth mode is opt-in via sandy_config; fail-open to False on any
    config blip (stealth is an upgrade, never a requirement)."""
    try:
        return bool(config.get_config("scrapling_stealth"))
    except Exception:
        return False


def fetch(url: str, mode: str = "fast") -> dict:
    """Fetch a URL and return LLM-ready markdown. Structured err
    return on every failure -- never raises into callers."""
    if not _IMPORT_OK:
        log(f"[scraply] scrapling import failed: {_IMPORT_ERROR!r}")
        return {"ok": False, "url": url, "error": "scrapling not available in this environment"}

    if mode == "stealth" or (mode == "auto" and stealth_enabled()):
        return _fetch_stealth(url)
    return _fetch_fast(url)


def _fetch_fast(url: str) -> dict:
    """Plain HTTP fetch via FetcherSession -- fast and cheap, no browser."""
    try:
        session = FetcherSession(timeout=FETCH_TIMEOUT)
        page = session.fetch(url)
        raw = page.body[:MAX_BYTES]
        status = page.status
        title = ""
        try:
            title = page.dom.title() or ""
        except Exception:
            pass
        markdown = page.dom.to_markdown()[:MAX_MARKDOWN_CHARS] if title != "(untitled)" else ""
        if not markdown:
            try:
                from markify import markify
                markdown = markify(raw)[:MAX_MARKDOWN_CHARS]
            except Exception:
                markdown = raw.decode("utf-8", errors="replace")[:MAX_MARKDOWN_CHARS]
        return {"ok": True, "url": url, "title": title, "markdown": markdown, "status": status}
    except Exception as e:
        log(f"[scraply/fast] fetch failed for {url}: {e!r}")
        return {"ok": False, "url": url, "error": str(e)}


def _fetch_stealth(url: str) -> dict:
    """Real browser fetch via StealthyFetcher -- solves Cloudflare, slower."""
    try:
        fetcher = StealthyFetcher()
        page = fetcher.fetch(url)
        raw = page.body[:MAX_BYTES]
        status = page.status
        title = ""
        try:
            title = page.dom.title() or ""
        except Exception:
            pass
        markdown = page.dom.to_markdown()[:MAX_MARKDOWN_CHARS] if title != "(untitled)" else ""
        if not markdown:
            try:
                from markify import markify
                markdown = markify(raw)[:MAX_MARKDOWN_CHARS]
            except Exception:
                markdown = raw.decode("utf-8", errors="replace")[:MAX_MARKDOWN_CHARS]
        return {"ok": True, "url": url, "title": title, "markdown": markdown, "status": status}
    except Exception as e:
        log(f"[scraply/stealth] fetch failed for {url}: {e!r}")
        return {"ok": False, "url": url, "error": str(e)}


if __name__ == "__main__":
    # Smoke test -- requires a real URL
    result = fetch("https://example.com")
    assert result["ok"], f"example.com failed: {result}"
    assert "markdown" in result
    print(f"scraply.py: fetch OK -> {result['title']!r}")
