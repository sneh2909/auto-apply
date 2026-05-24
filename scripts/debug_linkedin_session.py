"""Debug LinkedIn session — prints the URL after navigating to /feed/."""

from __future__ import annotations

import asyncio

from autoapply.config import get_settings


async def main() -> None:
    from playwright.async_api import async_playwright

    settings = get_settings()
    print(f"Profile dir: {settings.linkedin_browser_dir}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(settings.linkedin_browser_dir),
            headless=settings.headless_browser,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = await ctx.new_page()
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)

        print(f"\nFinal URL: {page.url}")
        print(f"Page title: {await page.title()}")

        if "/feed" in page.url:
            print("\nResult: LOGGED IN — session is valid")
        else:
            print("\nResult: NOT LOGGED IN — re-run setup_linkedin_login.py")

        await asyncio.sleep(3)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
