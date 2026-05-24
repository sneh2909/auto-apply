"""Instahyre.com job scraper — browser agent.

Instahyre specialises in startup and product-company ML/AI roles in India.
Job search works without login; however, direct application requires login.
The scraper extracts job listings and emails for the pipeline to use.

Search URL: https://www.instahyre.com/search-jobs/?job_title=<role>&location=<loc>
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


class InstahyreScraper(Source):
    """Searches Instahyre for ML/AI jobs via Gemini browser agent."""

    name = "scrape:instahyre"

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        profile = get_profile()

        roles = profile_roles(profile)
        locations = profile_locations(profile)

        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()
        records: list[JobRecord] = []

        try:
            async with BrowserSession(headless=settings.headless_browser) as browser:
                agent = build_job_agent(browser, portal="instahyre", settings=settings)
                records = await agent.run(roles, locations)
        finally:
            run.jobs_emitted = len(records)
            run.finished_at = datetime.now(UTC)
            run.notes = "jobs persisted during agent save_job calls"
            await run.save()

        return records
