"""LinkedIn job scraper — DOM-first for jobs + hiring posts.

Uses Playwright with your logged-in profile:
  1. LinkedIn Jobs search (past week, newest first) — structured listings + JD pages
  2. LinkedIn Content search — recruiter hiring posts with HR emails
  3. Google Jobs (optional) — open-web discovery for profile role + city combos

LLM browser agent is only used as fallback when DOM extraction finds nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from autoapply.config import get_profile, get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import JobRecord, Source, upsert_jobs
from autoapply.sources.browser_agent.agent import build_job_agent
from autoapply.sources.browser_agent.browser import BrowserSession
from autoapply.sources.scrape_utils import profile_locations, profile_roles
from autoapply.sources.scrapers.google_jobs import GoogleJobsScraper
from autoapply.sources.scrapers.linkedin_jobs_dom import LinkedInJobsDomScraper
from autoapply.sources.scrapers.linkedin_posts import LinkedInPostsScraper

log = logging.getLogger(__name__)


class LinkedInScraper(Source):
    """LinkedIn Jobs + hiring posts + optional Google Jobs web discovery."""

    name = "scrape:linkedin"

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        profile = get_profile()

        roles = profile_roles(profile)
        locations = profile_locations(profile)

        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()
        records: list[JobRecord] = []

        try:
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=settings.linkedin_browser_dir,
            ) as browser:
                logged_in = await self._check_login(browser)
                if not logged_in:
                    run.notes = "not logged in — run scripts/setup_linkedin_login.py"
                    log.error(run.notes)
                else:
                    # 1. Structured LinkedIn Jobs (DOM)
                    jobs_dom = LinkedInJobsDomScraper()
                    dom_jobs = await jobs_dom.fetch(
                        browser,
                        roles,
                        locations,
                        tpr=settings.linkedin_jobs_tpr,
                        max_per_search=settings.linkedin_max_jobs_per_search,
                        fetch_details=settings.linkedin_fetch_job_pages,
                    )
                    records.extend(dom_jobs)
                    log.info("linkedin DOM jobs: %d", len(dom_jobs))

                    # 2. Hiring posts with emails (DOM)
                    posts = LinkedInPostsScraper()
                    from autoapply.sources.scrape_utils import build_linkedin_content_queries

                    post_records = await posts.scrape_with_browser(
                        browser,
                        build_linkedin_content_queries(profile),
                        scroll_pages=settings.linkedin_scroll_pages,
                        fetch_post_pages=settings.linkedin_fetch_post_pages,
                        max_per_query=settings.linkedin_max_posts_per_query,
                    )
                    records.extend(post_records)
                    log.info("linkedin posts: %d", len(post_records))

                    # 3. Open-web Google Jobs discovery
                    if settings.web_discovery_enabled:
                        google = GoogleJobsScraper()
                        records.extend(await google.fetch(browser))

                    # 4. LLM agent fallback if DOM found very little
                    if len(records) < settings.linkedin_agent_fallback_min:
                        log.info(
                            "linkedin DOM found %d jobs — running LLM agent fallback",
                            len(records),
                        )
                        agent = build_job_agent(browser, portal="linkedin", settings=settings)
                        records.extend(await agent.run(roles[:2], locations[:2]))

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
            await browser.navigate("https://www.linkedin.com/feed/", delay=2.5)
            url = browser.url
            return "/feed" in url and "/login" not in url and "authwall" not in url
        except Exception:
            return False
