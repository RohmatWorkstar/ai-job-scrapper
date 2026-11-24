"""Optional headless-browser rendering for JavaScript-heavy career pages (Playwright).

Disabled by default (``extraction.use_browser_rendering``). When enabled:
* robots.txt and SSRF checks are applied before navigation (same as plain fetches),
* every sub-request is checked; requests to non-public addresses are aborted,
* images/media/fonts are blocked and downloads are refused,
* no login, CAPTCHA solving or anti-bot evasion is attempted.
If Playwright is not installed the renderer reports itself unavailable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.core.exceptions import UnsafeURLError
from src.utils.logger import get_logger, log_event
from src.utils.validators import validate_url_safe

log = get_logger(__name__)


class BrowserRenderer:
    _instance: "BrowserRenderer | None" = None

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self.unavailable_reason: str | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    @classmethod
    def shared(cls) -> "BrowserRenderer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def is_installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def _ensure(self) -> bool:
        if self._browser is not None:
            return True
        if self.unavailable_reason:
            return False
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.unavailable_reason = "Playwright not installed (pip install playwright && python -m playwright install chromium)"
            return False
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            return True
        except Exception as exc:  # noqa: BLE001
            self.unavailable_reason = f"Browser launch failed: {type(exc).__name__}"
            log_event(log, 30, "browser.unavailable", error=repr(exc))
            return False

    async def render(self, url: str, http: Any, timeout_ms: int = 25000) -> str | None:
        try:
            await validate_url_safe(url)
        except UnsafeURLError:
            return None
        if http is not None and not await http.robots_allowed(url):
            return None
        async with self._get_lock():
            if not await self._ensure():
                return None
            context = await self._browser.new_context(accept_downloads=False, java_script_enabled=True)
            try:
                page = await context.new_page()

                async def guard(route: Any) -> None:
                    req = route.request
                    if req.resource_type in {"image", "media", "font"}:
                        await route.abort()
                        return
                    try:
                        await validate_url_safe(req.url)
                    except UnsafeURLError:
                        await route.abort()
                        return
                    await route.continue_()

                await page.route("**/*", guard)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 - best effort
                    pass
                return await page.content()
            except Exception as exc:  # noqa: BLE001
                log_event(log, 30, "browser.render_failed", url=url, error=type(exc).__name__)
                return None
            finally:
                await context.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = self._pw = None
