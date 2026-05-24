"""Foundit.in (formerly Monster India) job scraper — browser agent.

Foundit is strong for mid-to-senior ML/AI roles in India. No login required.
Search URL: https://www.foundit.in/srp/results?query=<role>&location=<loc>&sort=1
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


class FounditScraper(Source):
    """Searches Foundit.in for ML/AI jobs via Gemini browser agent."""

    name = "scrape:foundit"

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
                agent = build_job_agent(browser, portal="foundit", settings=settings)
                records = await agent.run(roles, locations)
        finally:
            run.jobs_emitted = len(records)
            run.finished_at = datetime.now(UTC)
            run.notes = "jobs persisted during agent save_job calls"
            await run.save()

        return records
