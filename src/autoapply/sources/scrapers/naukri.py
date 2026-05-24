"""Naukri.com job scraper — browser agent, no DOM selectors.

Naukri is the largest job board in India for ML/AI roles. No login required
for search. The Gemini agent navigates to search results pages, reads all
visible job listings, follows job detail links, extracts emails, and saves jobs.

Naukri search URL pattern:
  https://www.naukri.com/jobs-in-<location-slug>?k=<role>&experience=0
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from autoapply.config import get_profile, get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import JobRecord, Source
from autoapply.sources.browser_agent.agent import build_job_agent
from autoapply.sources.browser_agent.browser import BrowserSession
from autoapply.sources.scrape_utils import profile_locations, profile_roles

log = logging.getLogger(__name__)


class NaukriScraper(Source):
    """Searches Naukri.com for ML/AI jobs via Gemini browser agent."""

    name = "scrape:naukri"

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        profile = get_profile()

        roles = profile_roles(profile)
        locations = profile_locations(profile)
        if profile.target.remote_ok and "Work from home" not in locations:
            locations = [*locations, "Work from home"]

        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()
        records: list[JobRecord] = []

        try:
            async with BrowserSession(headless=settings.headless_browser) as browser:
                agent = build_job_agent(browser, portal="naukri", settings=settings)
                records = await agent.run(roles, locations)
        finally:
            run.jobs_emitted = len(records)
            run.finished_at = datetime.now(UTC)
            run.notes = "jobs persisted during agent save_job calls"
            await run.save()

        return records
