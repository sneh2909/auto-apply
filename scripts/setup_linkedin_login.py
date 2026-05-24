"""Bootstrap LinkedIn auth for the scraper.

Opens a non-headless Chromium with the persistent profile dir, navigates to
LinkedIn login, and waits for you to sign in (handle 2FA / captchas yourself).
When you reach the feed, hit ENTER in the terminal. The session cookies persist
in `data/linkedin-profile/` and the scraper reuses them on every run.

If LinkedIn signs you out later (typical: every 4-8 weeks, or on suspicious
activity), just re-run this script.
"""

from __future__ import annotations

import asyncio
import sys

from autoapply.config import get_settings


async def main() -> int:
    from playwright.async_api import async_playwright

    settings = get_settings()
    settings.ensure_dirs()
    profile_dir = settings.linkedin_browser_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"Opening Chromium with profile dir: {profile_dir}")
    print("Sign in to LinkedIn in the browser window. Solve any 2FA/captcha yourself.")
    print("When you can see your feed, come back here and press ENTER.\n")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        # Block until the user presses Enter.
        await asyncio.to_thread(input, "Press ENTER once you're logged in… ")

        # Quick sanity check.
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        url = page.url
        if "/login" in url or "/checkpoint" in url:
            print("ERROR: still on a login/checkpoint page — sign-in didn't take.", file=sys.stderr)
            await ctx.close()
            return 1

        print(f"\nOK — session saved to {profile_dir}")
        print("Test it with:  autoapply ingest")
        await ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
