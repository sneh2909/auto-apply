"""Google Jobs web discovery — profile-driven role + location search.

Uses the Google Jobs tab (udm=8) to find recently posted roles across the
open web, not limited to a single job board.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

from autoapply.config import get_profile, get_settings
from autoapply.sources.llm_extract import llm_enrich_record
from autoapply.sources.base import JobRecord
from autoapply.sources.browser_agent.browser import BrowserSession
from autoapply.sources.scrape_utils import (
    build_role_location_combos,
    extract_email,
    extract_location,
)

log = logging.getLogger(__name__)

_EXTRACT_JS = r"""
() => {
  const cards = document.querySelectorAll(
    'div[data-share-url], div.PwjeAc, div.BjJfJf, li.iFjDol'
  );
  const out = [];
  const seen = new Set();
  for (const card of cards) {
    const titleEl = card.querySelector('h2, .BjJfJf, [role="heading"]');
    const companyEl = card.querySelector('.vNEEBe, .Qk80Jf, span[class*="company"]');
    const locEl = card.querySelector('.Qk80Jf + span, .sMzDkb, span[class*="location"]');
    const linkEl = card.querySelector('a[href*="/url?q="], a[href*="http"]');
    const title = titleEl ? titleEl.innerText.trim() : '';
    const company = companyEl ? companyEl.innerText.trim() : '';
    const location = locEl ? locEl.innerText.trim() : '';
    let href = linkEl ? linkEl.href : '';
    if (href.includes('/url?q=')) {
      try {
        const u = new URL(href);
        href = u.searchParams.get('q') || href;
      } catch (e) {}
    }
    const key = title + '|' + company;
    if (!title || title.length < 4 || seen.has(key)) continue;
    seen.add(key);
    out.push({ title, company, location, href });
    if (out.length >= 20) break;
  }
  // Fallback: job-like links in page
  if (out.length === 0) {
    for (const a of document.querySelectorAll('a[href]')) {
      const t = (a.innerText || '').trim();
      const h = a.href || '';
      if (t.length < 8 || t.length > 120) continue;
      if (!/engineer|scientist|developer|analyst|manager/i.test(t)) continue;
      if (!/greenhouse|lever|ashby|workday|careers|jobs|naukri|foundit|instahyre/i.test(h)) continue;
      const key = t + h;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ title: t, company: '', location: '', href: h });
      if (out.length >= 15) break;
    }
  }
  return out;
}
"""


class GoogleJobsScraper:
    name = "scrape:google_jobs"

    async def fetch(self, browser: BrowserSession) -> list[JobRecord]:
        if not get_settings().web_discovery_enabled:
            return []

        profile = get_profile()
        combos = build_role_location_combos(profile)
        records: list[JobRecord] = []
        seen: set[str] = set()

        for role, location in combos[:12]:
            try:
                batch = await self._search(browser, role, location, seen)
                records.extend(batch)
            except Exception:
                log.exception("google jobs failed for %r in %r", role, location)
            await asyncio.sleep(random.uniform(2.5, 4.5))

        log.info("google jobs discovery: %d records", len(records))
        return records

    async def _search(
        self,
        browser: BrowserSession,
        role: str,
        location: str,
        seen: set[str],
    ) -> list[JobRecord]:
        q = quote_plus(f"{role} jobs {location} India posted last week")
        url = f"https://www.google.com/search?q={q}&udm=8"
        log.info("google jobs → %s", url)
        await browser.navigate(url, delay=4.0)
        await browser.scroll_load(times=2, pause=1.5)

        items: list[dict[str, Any]] = []
        try:
            items = await browser.page.evaluate(_EXTRACT_JS)
        except Exception as e:
            log.warning("google jobs extract failed: %s", e)
            return []

        records: list[JobRecord] = []
        for item in items:
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "Unknown").strip() or "Unknown"
            loc = (item.get("location") or location).strip() or None
            href = (item.get("href") or "").strip() or None
            key = f"{title}|{company}|{href or ''}"
            if key in seen:
                continue
            seen.add(key)

            sid = hashlib.sha1(key.encode()).hexdigest()[:16]
            jd_text = f"{title} at {company}. {loc or ''}".strip()
            hr_email: str | None = None

            if href and href.startswith("http"):
                try:
                    page_text = await browser.navigate(href, delay=2.5)
                    hr_email = extract_email(page_text)
                    if len(page_text) > len(jd_text):
                        jd_text = page_text[:4000]
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(1.0, 2.0))

            record = JobRecord(
                source="scrape:google_jobs",
                source_id=sid,
                company=company,
                role=title,
                jd_text=jd_text,
                jd_url=href,
                location=loc or extract_location(jd_text),
                remote=bool(loc and "remote" in loc.lower()),
                hr_email=hr_email,
                posted_at=datetime.now(UTC),
                raw_payload={"search_role": role, "search_location": location},
            )
            if get_settings().scraper_llm_enrich:
                record = await llm_enrich_record(record, context="google_jobs")
            records.append(record)
        return records
