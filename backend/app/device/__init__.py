"""Device control facade.

Sandy can open and control WEB APPS (Gmail, Ads Manager, Replit, GitHub,
Twitter, LinkedIn, Replit bounties, etc.) through a persistent Playwright
browser. NOT OS-level control — that was Ruk's explicit choice: device
control is just "open the app and use it," not "bypass the OS."

Permission model:
  - First time Sandy sees a new domain, she asks Ruk to approve it
  - After approval, the domain is in the allowlist forever (until Ruk
    revokes)
  - Every action on an approved domain logs to device_sessions

Why browser automation first, not native desktop automation:
  - Covers 90% of "apps Ruk uses" (everything's a web app now)
  - Works headless on HF Spaces (no display, no GUI)
  - No sandboxing/permissions issues
  - Easy to revert (close the tab)
  - No risk of "Sandy accidentally does something destructive on the
    laptop" — she's only touching a browser tab

Phase 2 will add: scrcpy for Android phone mirroring, Appium for mobile
apps. Not in this build.
"""
import logging

import config

log = logging.getLogger("sandy.device")


class DeviceController:
    """Thin facade. The actual Playwright work is in browser.py. Mobile /
    desktop expansion goes in mobile.py / desktop.py following the same
    interface."""

    def __init__(self):
        self._browser = None  # lazy: BrowserController created on first action

    def _c(self):
        return config.get_client()

    async def open_app(self, app_name_or_url: str) -> dict:
        """Open an app or navigate to a URL. If a domain is new, returns
        a 'permission_required' status instead of acting — the chat
        handler in main.py is expected to surface that to Ruk."""
        from device import browser, permissions
        from urllib.parse import urlparse

        url = browser.APP_URLS.get(app_name_or_url.lower(), app_name_or_url)
        if not url.startswith("http"):
            url = "https://" + url
        domain = urlparse(url).netloc

        if not permissions.is_approved(domain):
            return {
                "ok": False,
                "permission_required": True,
                "domain": domain,
                "message": f"Sandy wants to open {domain} for the first time. Ruk, approve?",
            }

        controller = browser.get_browser()
        page = await controller.navigate(url)
        return {"ok": True, "url": url, "domain": domain, "page_title": await page.title()}

    async def click(self, selector: str) -> dict:
        from device import browser
        return await browser.get_browser().click(selector)

    async def type_text(self, selector: str, text: str) -> dict:
        from device import browser
        return await browser.get_browser().type(selector, text)

    async def screenshot(self) -> bytes:
        from device import browser
        return await browser.get_browser().screenshot()

    async def page_content(self) -> str:
        from device import browser
        return await browser.get_browser().get_text()

    async def list_open_tabs(self) -> list[dict]:
        from device import browser
        return await browser.get_browser().list_tabs()

    async def close_browser(self) -> None:
        from device import browser
        await browser.shutdown()


_controller: DeviceController | None = None


def get_device() -> DeviceController:
    global _controller
    if _controller is None:
        _controller = DeviceController()
    return _controller
