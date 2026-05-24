"""DOM-based LinkedIn Jobs search scraper (no LLM).

Loads /jobs/search with past-week + newest sort, extracts listing cards,
opens each /jobs/view/<id> page for full description and recruiter email.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import quote_plus

from autoapply.sources.base import JobRecord
from autoapply.sources.browser_agent.browser import BrowserSession
from autoapply.config import get_settings
from autoapply.sources.llm_extract import llm_enrich_record
from autoapply.sources.scrape_utils import extract_all_emails, extract_email, extract_location

log = logging.getLogger(__name__)

_JOBS_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords={role}&location={loc}&f_TPR={tpr}&sortBy=DD"
)

_EXTRACT_LISTINGS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const anchors = document.querySelectorAll(
    'a[href*="/jobs/view/"], a.base-card__full-link, a.job-card-container__link'
  );
  for (const a of anchors) {
    const href = a.href || '';
    const m = href.match(/jobs\/view\/(\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const card = a.closest('li, div.base-card, div.job-card-container') || a.parentElement;
    const title = (a.innerText || '').trim().split('\n')[0].trim();
  let company = '';
  let location = '';
  let posted = '';
  if (card) {
    const companyEl = card.querySelector(
      '.base-search-card__subtitle, .job-card-container__company-name, h4'
    );
    const locEl = card.querySelector(
      '.job-search-card__location, .job-card-container__metadata-item'
    );
    const timeEl = card.querySelector('time, .job-search-card__listdate');
    company = companyEl ? companyEl.innerText.trim() : '';
    location = locEl ? locEl.innerText.trim() : '';
    posted = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '';
  }
    if (title.length < 3) continue;
    out.push({
      id: m[1],
      title,
      company,
      location,
      posted,
      url: href.split('?')[0],
    });
    if (out.length >= 30) break;
  }
  return out;
}
"""

_EXTRACT_JOB_PAGE_JS = r"""
() => {
  const title = (
    document.querySelector('h1, .job-details-jobs-unified-top-card__job-title, .t-24')?.innerText || ''
  ).trim();
  const company = (
    document.querySelector(
      '.job-details-jobs-unified-top-card__company-name a, .jobs-unified-top-card__company-name a'
    )?.innerText || ''
  ).trim();
  const location = (
    document.querySelector(
      '.job-details-jobs-unified-top-card__bullet, .jobs-unified-top-card__bullet'
    )?.innerText || ''
  ).trim();
  const desc = (
    document.querySelector(
      '#job-details, .jobs-description__content, .jobs-box__html-content'
    )?.innerText || document.body.innerText
  ).slice(0, 6000);
  const mailtos = [...document.querySelectorAll('a[href^="mailto:"]')]
    .map(a => a.href.replace('mailto:', '').split('?')[0]);
  return { title, company, location, desc, mailtos };
}
"""


class LinkedInJobsDomScraper:
    """Extract structured jobs from LinkedIn Jobs search via Playwright DOM."""

    async def fetch(
        self,
        browser: BrowserSession,
        roles: list[str],
        locations: list[str],
        *,
        tpr: str = "r604800",
        max_per_search: int = 12,
        fetch_details: bool = True,
    ) -> list[JobRecord]:
        records: list[JobRecord] = []
        seen_ids: set[str] = set()

        for role in roles:
            for loc in locations:
                try:
                    batch = await self._scrape_search(
                        browser,
                        role,
                        loc,
                        tpr=tpr,
                        max_jobs=max_per_search,
                        fetch_details=fetch_details,
                        seen_ids=seen_ids,
                    )
                    records.extend(batch)
                except Exception:
                    log.exception("linkedin jobs DOM failed for %r in %r", role, loc)
                await asyncio.sleep(random.uniform(2.0, 4.0))

        return records

    async def _scrape_search(
        self,
        browser: BrowserSession,
        role: str,
        location: str,
        *,
        tpr: str,
        max_jobs: int,
        fetch_details: bool,
        seen_ids: set[str],
    ) -> list[JobRecord]:
        url = _JOBS_SEARCH_URL.format(
            role=quote_plus(role),
            loc=quote_plus(location),
            tpr=tpr,
        )
        log.info("linkedin jobs DOM → %s", url)
        await browser.navigate(url, delay=4.0)
        await browser.scroll_load(times=4, pause=1.8)

        listings: list[dict[str, Any]] = []
        try:
            listings = await browser.page.evaluate(_EXTRACT_LISTINGS_JS)
        except Exception as e:
            log.warning("linkedin jobs listing extract failed: %s", e)
            return []

        records: list[JobRecord] = []
        for item in listings[:max_jobs]:
            jid = str(item.get("id") or "")
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)

            title = (item.get("title") or role).strip()
            company = (item.get("company") or "Unknown").strip() or "Unknown"
            loc = (item.get("location") or location).strip() or None
            job_url = item.get("url") or f"https://www.linkedin.com/jobs/view/{jid}/"
            jd_text = f"{title} at {company}. {loc or ''}".strip()
            hr_email: str | None = None

            if fetch_details:
                detail = await self._fetch_job_page(browser, job_url)
                if detail:
                    title = detail.get("title") or title
                    company = detail.get("company") or company
                    loc = detail.get("location") or loc
                    jd_text = (detail.get("desc") or jd_text)[:6000]
                    mailtos = detail.get("mailtos") or []
                    hr_email = mailtos[0] if mailtos else extract_email(jd_text)
                await asyncio.sleep(random.uniform(1.0, 2.0))

            record = JobRecord(
                source="scrape:linkedin",
                source_id=jid,
                company=company,
                role=title,
                jd_text=jd_text,
                jd_url=job_url,
                location=loc or extract_location(jd_text),
                remote=bool(loc and "remote" in loc.lower()),
                hr_email=hr_email,
                raw_payload={"search_role": role, "search_location": location},
            )
            if get_settings().scraper_llm_enrich:
                record = await llm_enrich_record(record, context="linkedin_job_page")
            records.append(record)

        log.info("linkedin jobs DOM: %d jobs from %r / %r", len(records), role, location)
        return records

    @staticmethod
    async def _fetch_job_page(browser: BrowserSession, url: str) -> dict[str, Any] | None:
        try:
            await browser.navigate(url, delay=3.0)
            if "/login" in browser.url or "authwall" in browser.url:
                return None
            data = await browser.page.evaluate(_EXTRACT_JOB_PAGE_JS)
            if not data.get("desc"):
                text = await browser.get_text(8000)
                data["desc"] = text
            emails = extract_all_emails(data.get("desc") or "")
            if emails and not data.get("mailtos"):
                data["mailtos"] = emails
            return data
        except Exception as e:
            log.debug("job page fetch failed %s: %s", url, e)
            return None
