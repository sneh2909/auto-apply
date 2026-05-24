#!/usr/bin/env python3
"""One-time interactive login helper for job portals.

Opens a headful Chromium window pointed at the portal login page.
You log in manually, then close the window — the session cookies are
saved in the persistent profile directory and reused for future apply runs.

Usage:
    python scripts/setup_portal_login.py naukri
    python scripts/setup_portal_login.py foundit
    python scripts/setup_portal_login.py instahyre
    python scripts/setup_portal_login.py linkedin
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Resolve project root so we can import autoapply without installing
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


_PORTALS: dict[str, tuple[str, str]] = {
    "naukri":    ("naukri_browser_dir",    "https://www.naukri.com/nlogin/login"),
    "foundit":   ("foundit_browser_dir",   "https://www.foundit.in/login"),
    "instahyre": ("instahyre_browser_dir", "https://www.instahyre.com/login"),
    "linkedin":  ("linkedin_browser_dir",  "https://www.linkedin.com/login"),
}


async def main(portal: str) -> None:
    from autoapply.config import get_settings
    from autoapply.sources.browser_agent.browser import BrowserSession

    if portal not in _PORTALS:
        print(f"Unknown portal '{portal}'. Choose from: {', '.join(_PORTALS)}")
        sys.exit(1)

    dir_attr, login_url = _PORTALS[portal]
    settings = get_settings()
    profile_dir: Path = getattr(settings, dir_attr)
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Opening {portal.title()} login page in a browser window.")
    print(f"  Profile dir: {profile_dir}")
    print(f"  1. Log in with your credentials.")
    print(f"  2. Close the browser window when done.")
    print(f"{'='*60}\n")

    async with BrowserSession(headless=False, profile_dir=profile_dir) as b:
        await b.navigate(login_url, wait="domcontentloaded", delay=0)
        print("Browser opened. Waiting for you to log in and close the window…")
        # Poll until the context disconnects (user closed the window).
        # Avoids wait_for_event("close") which races with __aexit__ teardown.
        try:
            while True:
                try:
                    if b.page.is_closed():
                        break
                except Exception:
                    break
                await asyncio.sleep(1.5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    print(f"\nDone! Session saved to {profile_dir}")
    print("You can now run the pipeline and it will use this session for apply automation.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/setup_portal_login.py <portal>")
        print(f"Portals: {', '.join(_PORTALS)}")
        sys.exit(1)

    asyncio.run(main(sys.argv[1].lower()))
