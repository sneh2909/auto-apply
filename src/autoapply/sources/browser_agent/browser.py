"""Thin Playwright wrapper for browser-based job scraping.

Provides navigate / get_text / get_links / scroll — no CSS selectors, no
class-name hunting. Text extraction uses inner_text("body") which is stable
across site redesigns. Links are extracted via a universal querySelectorAll("a")
(href attribute is not a class name — it won't change).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


class BrowserSession:
    """
    Context-manager wrapping a single Playwright browser page.

    Usage::

        async with BrowserSession(headless=True) as b:
            text = await b.navigate("https://naukri.com/jobs?k=ml")
            links = await b.get_links()
    """

    def __init__(self, *, headless: bool = True, profile_dir: Path | None = None):
        """
        Args:
            headless:    run without a visible window
            profile_dir: if set, launch a persistent context that persists cookies
                         across sessions (required for LinkedIn login)
        """
        self._headless = headless
        self._profile_dir = profile_dir
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    async def __aenter__(self) -> BrowserSession:
        from playwright.async_api import async_playwright

        if self._profile_dir:
            # Remove stale Chrome singleton lock left by a crashed session
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                p = Path(self._profile_dir) / lock
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

        self._playwright = await async_playwright().start()
        if self._profile_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                viewport={"width": 1280, "height": 900},
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=_STEALTH_ARGS,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=_USER_AGENT,
                ignore_https_errors=True,
            )
        self.page = await self._context.new_page()
        return self

    async def __aexit__(self, *_: object) -> None:
        # Swallow TargetClosedError — common when the user manually closes the
        # browser window before us (esp. setup_portal_login.py interactive flow).
        for resource, closer in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                await getattr(resource, closer)()
            except Exception as exc:
                log.debug("ignoring %s on %s teardown: %s", type(exc).__name__, closer, exc)

    # ------------------------------------------------------------------

    async def navigate(self, url: str, *, wait: str = "domcontentloaded", delay: float = 3.5) -> str:
        """Go to *url*, wait for JS render, return full body text."""
        try:
            await self.page.goto(url, wait_until=wait, timeout=30_000)
            if delay > 0:
                await asyncio.sleep(delay)
        except Exception:
            log.warning("navigate timeout/error for %s", url)
        return await self.get_text()

    async def get_text(self, max_chars: int = 6000) -> str:
        """All visible text on the current page via inner_text('body')."""
        try:
            return (await self.page.inner_text("body"))[:max_chars]
        except Exception:
            return ""

    async def get_links(self) -> list[dict]:
        """Return [{text, href}] for every <a href> on the current page.

        Uses universal querySelectorAll — not fragile class names.
        """
        try:
            return await self.page.evaluate(
                """() => [...document.querySelectorAll('a[href]')].map(a => ({
                    text: (a.textContent || a.innerText || '').trim().slice(0, 150),
                    href: a.href
                })).filter(l => l.href.startsWith('http') && l.text.length > 1)"""
            )
        except Exception:
            return []

    async def scroll_load(self, *, times: int = 4, pause: float = 1.5) -> str:
        """Scroll down *times* x one viewport to trigger lazy-load; return new text."""
        for _ in range(times):
            try:
                await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
            except Exception:
                break
            await asyncio.sleep(pause)
        return await self.get_text()

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""
