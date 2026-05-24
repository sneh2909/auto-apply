"""LinkedIn job scraper — browser agent, no brittle DOM selectors.

Searches both LinkedIn Jobs (structured listings) and LinkedIn Content (hiring
posts with recruiter emails). Uses a persistent browser profile that keeps the
LinkedIn session alive between runs (bootstrapped by scripts/setup_linkedin_login.py).

The Gemini agent navigates to the exact search URLs, reads the full page text,
follows individual job links, extracts emails, and saves each job.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from autoapply.config import get_profile, get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import JobRecord, Source, upsert_jobs
from autoapply.sources.browser_agent.agent import build_job_agent
from autoapply.sources.browser_agent.browser import BrowserSession

log = logging.getLogger(__name__)


class LinkedInScraper(Source):
    """Searches LinkedIn Jobs + LinkedIn hiring posts via Gemini browser agent."""

    name = "scrape:linkedin"

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        profile = get_profile()

        roles = (profile.target.roles or ["Machine Learning Engineer"])[:4]
        locations = (profile.target.on_site_cities_ok or ["Bangalore"])[:3]
        if profile.target.remote_ok:
            locations = [*locations, "Remote"]

        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()
        records: list[JobRecord] = []

        try:
            # LinkedIn requires a logged-in persistent profile
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=settings.linkedin_browser_dir,
            ) as browser:
                # Check login state first
                logged_in = await self._check_login(browser)
                if not logged_in:
                    run.notes = "not logged in — run scripts/setup_linkedin_login.py"
                    log.error(run.notes)
                else:
                    # Agent 1: LinkedIn Jobs (structured job listings)
                    jobs_agent = build_job_agent(browser, portal="linkedin", settings=settings)
                    records.extend(await jobs_agent.run(roles, locations))

                    # Agent 2: LinkedIn Posts (recruiter hiring posts with HR emails)
                    # Use a separate agent so the conversation history stays focused
                    posts_agent = build_job_agent(browser, portal="linkedin_posts", settings=settings)
                    records.extend(await posts_agent.run(roles, locations))

        finally:
            inserted, skipped = await upsert_jobs(records)
            run.jobs_emitted = len(records)
            run.finished_at = datetime.now(UTC)
            run.notes = (run.notes + " | " if run.notes else "") + f"inserted={inserted} skipped={skipped}"
            await run.save()

        return records

    @staticmethod
    async def _check_login(browser: BrowserSession) -> bool:
        try:
            await browser.navigate("https://www.linkedin.com/feed/", wait="domcontentloaded", delay=2.0)
            url = browser.url
            return "/feed" in url and "/login" not in url and "authwall" not in url
        except Exception:
            return False
