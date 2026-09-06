"""Playwright-based browser controller for web-app automation.

Persistent Chromium context (one browser, multiple tabs). Async API so
the FastAPI handler can stay non-blocking while Sandy waits for pages
to load / form submissions / etc.

App shortcuts live in APP_URLS — these are the apps Ruk actually uses
day-to-day that Sandy can open with a single word ("gmail", "ads",
"replit", "github", "twitter", "linkedin"). Anything not in the map
gets navigated to as a URL.

Why persistent context (not launch each call):
  - Cookies / logins survive between sessions
  - Faster (no Chromium startup every call)
  - Closer to "Ruk's actual browser" — same login state

Phase 2: scrcpy for Android, Appium for native mobile apps. Out of
scope for the 1-week build.
"""
import asyncio
import logging
import os
from typing import Any

log = logging.getLogger("sandy.device.browser")

# Lazy import: playwright is heavy and only needed when the user actually
# triggers a device-control action. Importing at module top would slow
# down every chat call that doesn't use it.
async def _playwright():
    from playwright.async_api import async_playwright
    return await async_playwright().start()


APP_URLS = {
    "gmail": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "ads": "https://ads.google.com",
    "ads manager": "https://ads.google.com",
    "google ads": "https://ads.google.com",
    "replit": "https://replit.com",
    "replit bounties": "https://replit.com/bounties",
    "bounties": "https://replit.com/bounties",
    "github": "https://github.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "linkedin": "https://linkedin.com",
    "youtube": "https://youtube.com",
    "notion": "https://notion.so",
    "figma": "https://figma.com",
    "drive": "https://drive.google.com",
}


class BrowserController:
    def __init__(self):
        self._pw = None
        self._context = None
        self._pages: list = []
        self._active_idx = 0
        self._lock = asyncio.Lock()

    async def _ensure_started(self):
        if self._context is None:
            self._pw = await _playwright()
            user_data_dir = os.environ.get("SANDY_BROWSER_PROFILE", "/tmp/sandy-chrome")
            self._context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            if self._context.pages:
                self._pages = list(self._context.pages)
            else:
                page = await self._context.new_page()
                self._pages = [page]

    async def navigate(self, url: str):
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx] if self._pages else await self._context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if page not in self._pages:
                self._pages.append(page)
            return page

    async def click(self, selector: str) -> dict:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            try:
                await page.click(selector, timeout=10_000)
                return {"ok": True, "selector": selector}
            except Exception as e:
                return {"ok": False, "error": str(e), "selector": selector}

    async def type(self, selector: str, text: str) -> dict:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            try:
                await page.fill(selector, text, timeout=10_000)
                return {"ok": True, "selector": selector, "text": text}
            except Exception as e:
                return {"ok": False, "error": str(e), "selector": selector}

    async def wait_for(self, selector: str, timeout_ms: int = 15_000) -> dict:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return {"ok": True, "selector": selector}
            except Exception as e:
                return {"ok": False, "error": str(e), "selector": selector}

    async def screenshot(self) -> bytes:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            return await page.screenshot(full_page=False)

    async def get_text(self, selector: str = "body") -> str:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            try:
                return await page.inner_text(selector, timeout=5_000)
            except Exception:
                return await page.inner_text("body", timeout=5_000)

    async def evaluate(self, script: str) -> Any:
        async with self._lock:
            await self._ensure_started()
            page = self._pages[self._active_idx]
            return await page.evaluate(script)

    async def new_tab(self, url: str = "about:blank") -> None:
        async with self._lock:
            await self._ensure_started()
            page = await self._context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._pages.append(page)
            self._active_idx = len(self._pages) - 1

    async def list_tabs(self) -> list[dict]:
        async with self._lock:
            await self._ensure_started()
            out = []
            for i, p in enumerate(self._pages):
                try:
                    out.append({"index": i, "url": p.url, "title": await p.title()})
                except Exception:
                    pass
            return out

    async def switch_tab(self, idx: int) -> None:
        async with self._lock:
            await self._ensure_started()
            if 0 <= idx < len(self._pages):
                self._active_idx = idx


_controller: BrowserController | None = None


def get_browser() -> BrowserController:
    global _controller
    if _controller is None:
        _controller = BrowserController()
    return _controller


async def shutdown() -> None:
    global _controller
    if _controller is not None:
        try:
            if _controller._context:
                await _controller._context.close()
            if _controller._pw:
                await _controller._pw.stop()
        except Exception as e:
            log.warning(f"browser shutdown error (non-fatal): {e!r}")
        _controller = None
