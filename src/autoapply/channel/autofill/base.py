"""Shared ATS auto-fill plumbing."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from autoapply.config import get_settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def persistent_browser(profile_dir: Path | None = None) -> Any:
    """Yield a (BrowserSession, page) backed by a persistent profile.

    Uses `sources.browser_agent.browser.BrowserSession` which already handles
    stale-lock cleanup and stealth args.
    """
    from autoapply.sources.browser_agent.browser import BrowserSession

    settings = get_settings()
    dir_ = profile_dir or settings.browser_profile_dir
    async with BrowserSession(headless=settings.headless_browser, profile_dir=dir_) as b:
        yield b


class ATSAutoFiller:
    """Per-ATS implementations override `apply()`."""

    name: str = "base"

    async def apply(self, application_id: str) -> None:
        raise NotImplementedError


def get_filler(ats_label: str) -> ATSAutoFiller | None:
    """Return the right AutoFiller for an ATS label like 'A:naukri', 'A:greenhouse', etc."""
    from autoapply.channel.autofill.foundit import FounditAutoFiller
    from autoapply.channel.autofill.greenhouse import GreenhouseAutoFiller
    from autoapply.channel.autofill.instahyre import InstahyreAutoFiller
    from autoapply.channel.autofill.lever import LeverAutoFiller
    from autoapply.channel.autofill.naukri import NaukriAutoFiller
    from autoapply.channel.autofill.workday import WorkdayAutoFiller

    _registry: dict[str, ATSAutoFiller] = {
        "naukri": NaukriAutoFiller(),
        "greenhouse": GreenhouseAutoFiller(),
        "lever": LeverAutoFiller(),
        "foundit": FounditAutoFiller(),
        "instahyre": InstahyreAutoFiller(),
        "workday": WorkdayAutoFiller(),
    }
    for key, filler in _registry.items():
        if key in ats_label.lower():
            return filler
    return None
