"""Scrape LinkedIn hiring posts via logged-in browser session.

Recruiters post hiring announcements with emails in the feed. DOM extraction
loads posts; the agent LLM extracts recruiter emails and structured job fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from autoapply.config import get_profile, get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import JobRecord, Source, upsert_jobs
from autoapply.sources.browser_agent.browser import BrowserSession
from autoapply.sources.llm_extract import llm_enrich_record, llm_records_from_text
from autoapply.sources.scrape_utils import (
    build_linkedin_content_queries,
    extract_company,
    extract_email,
    extract_linkedin_job_urls,
    extract_location,
    extract_role,
    looks_like_hiring,
    parse_relative_posted_at,
)

log = logging.getLogger(__name__)

_EXTRACTOR_JS = r"""
() => {
  const sel = (root, sels) => {
    for (const s of sels) {
      const el = root.querySelector(s);
      if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
    }
    return '';
  };
  const cards = document.querySelectorAll(
    '[data-urn^="urn:li:activity:"], div.feed-shared-update-v2, div[data-id^="urn:li:activity"]'
  );
  const out = [];
  for (const card of cards) {
    const urn = card.getAttribute('data-urn') || card.getAttribute('data-id') || '';
    const author = sel(card, [
      '.update-components-actor__title span[aria-hidden="true"]',
      '.update-components-actor__title',
      '.feed-shared-actor__title',
      'span.feed-shared-actor__name',
    ]);
    const subtitle = sel(card, [
      '.update-components-actor__description',
      '.feed-shared-actor__description',
      '.update-components-actor__sub-description',
    ]);
    const text = sel(card, [
      '.update-components-text',
      '.feed-shared-update-v2__description',
      '.feed-shared-text__text-view',
      '.feed-shared-inline-show-more-text',
      '.update-components-update-v2__commentary',
    ]);
    let postUrl = '';
    const link = card.querySelector('a[href*="/feed/update/"], a[href*="urn:li:activity"]');
    if (link) postUrl = link.href;
    const timeEl = card.querySelector('time, .update-components-actor__sub-description span');
    const postedHint = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '';
    if (!text || text.length < 40) continue;
    out.push({ urn, author, subtitle, text, postUrl, postedHint });
    if (out.length >= 50) break;
  }
  return out;
}
"""

_EXPAND_SEE_MORE_JS = r"""
() => {
  let clicked = 0;
  for (const btn of document.querySelectorAll('button')) {
    const t = (btn.innerText || '').toLowerCase();
    if (t.includes('see more') || t.includes('…more') || t.includes('show more')) {
      try { btn.click(); clicked++; } catch (e) {}
    }
    if (clicked >= 8) break;
  }
  return clicked;
}
"""


class LinkedInPostsScraper(Source):
    name = "scrape:linkedin_posts"

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        profile = get_profile()
        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()

        records: list[JobRecord] = []
        try:
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=settings.linkedin_browser_dir,
            ) as browser:
                if not await self._is_logged_in(browser):
                    run.notes = "not logged in — run scripts/setup_linkedin_login.py"
                    log.error(run.notes)
                else:
                    queries = build_linkedin_content_queries(profile)
                    records = await self.scrape_with_browser(
                        browser,
                        queries,
                        scroll_pages=settings.linkedin_scroll_pages,
                        fetch_post_pages=settings.linkedin_fetch_post_pages,
                        max_per_query=settings.linkedin_max_posts_per_query,
                    )
                    run.emails_seen = len(queries)
        finally:
            inserted, skipped = await upsert_jobs(records)
            run.jobs_emitted = len(records)
            run.finished_at = datetime.now(UTC)
            run.notes = (run.notes + " | " if run.notes else "") + f"inserted={inserted} skipped={skipped}"
            await run.save()

        return records

    async def scrape_with_browser(
        self,
        browser: BrowserSession,
        queries: list[str],
        *,
        scroll_pages: int = 5,
        fetch_post_pages: bool = True,
        max_per_query: int = 25,
    ) -> list[JobRecord]:
        records: list[JobRecord] = []
        seen_urns: set[str] = set()

        for query in queries:
            try:
                posts = await self._scrape_query(browser, query, scroll_pages=scroll_pages)
                for raw in posts:
                    urn = raw.get("urn") or ""
                    if urn and urn in seen_urns:
                        continue
                    if fetch_post_pages and raw.get("postUrl"):
                        raw = await self._enrich_post(browser, raw)
                    batch = await self._posts_from_raw(query, raw, max_per_query)
                    for rec in batch:
                        sid_key = urn or rec.source_id
                        if sid_key in seen_urns:
                            continue
                        seen_urns.add(sid_key)
                        records.append(rec)
                        for job_url in extract_linkedin_job_urls(rec.jd_text):
                            jid = re.search(r"/jobs/view/(\d+)", job_url)
                            if not jid:
                                continue
                            sid = jid.group(1)
                            if sid in seen_urns:
                                continue
                            seen_urns.add(sid)
                            records.append(
                                JobRecord(
                                    source="scrape:linkedin_posts",
                                    source_id=sid,
                                    company=rec.company,
                                    role=extract_role(rec.jd_text, fallback=query),
                                    jd_text=rec.jd_text[:2000],
                                    jd_url=job_url,
                                    location=rec.location,
                                    hr_email=rec.hr_email,
                                    recruiter_name=rec.recruiter_name,
                                    raw_payload={"from_post_urn": urn, "query": query},
                                )
                            )
            except Exception as e:
                log.exception("linkedin posts query %r failed: %s", query, e)
            await asyncio.sleep(random.uniform(2, 4))

        return records

    @staticmethod
    async def _is_logged_in(browser: BrowserSession) -> bool:
        try:
            await browser.navigate("https://www.linkedin.com/feed/", delay=2.5)
            url = browser.url
            return (
                "/feed" in url
                and "/login" not in url
                and "/checkpoint" not in url
                and "authwall" not in url
            )
        except Exception:
            return False

    @staticmethod
    async def _scrape_query(
        browser: BrowserSession,
        query: str,
        *,
        scroll_pages: int,
    ) -> list[dict[str, Any]]:
        url = (
            "https://www.linkedin.com/search/results/content/"
            f"?keywords={quote_plus(query)}&datePosted=%22past-week%22&sortBy=%22date_posted%22"
        )
        log.info("linkedin posts → %s", url)
        await browser.navigate(url, delay=4.0)

        try:
            await browser.page.evaluate(_EXPAND_SEE_MORE_JS)
            await asyncio.sleep(1.0)
        except Exception:
            pass

        for _ in range(scroll_pages):
            await browser.scroll_load(times=1, pause=random.uniform(1.2, 2.0))
            try:
                await browser.page.evaluate(_EXPAND_SEE_MORE_JS)
            except Exception:
                pass

        try:
            results = await browser.page.evaluate(_EXTRACTOR_JS)
        except Exception as e:
            log.warning("DOM extractor failed for %s: %s", query, e)
            return []

        if not results:
            try:
                dump_dir = get_settings().logs_dir
                dump_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^\w]+", "_", query[:30])
                dump_path = dump_dir / f"linkedin_posts_{slug}.html"
                html = await browser.page.content()
                dump_path.write_text(html[:20000], encoding="utf-8")
                log.warning("0 posts for %r — HTML snippet at %s", query, dump_path)
            except Exception:
                pass
        return results

    @staticmethod
    async def _enrich_post(browser: BrowserSession, raw: dict[str, Any]) -> dict[str, Any]:
        post_url = raw.get("postUrl") or ""
        if not post_url.startswith("http"):
            return raw
        try:
            await browser.navigate(post_url, delay=3.0)
            try:
                await browser.page.evaluate(_EXPAND_SEE_MORE_JS)
                await asyncio.sleep(0.8)
            except Exception:
                pass
            full_text = await browser.get_text(8000)
            if full_text and len(full_text) > len(raw.get("text") or ""):
                raw = {**raw, "text": full_text}
            raw["page_emails"] = extract_email(full_text)
        except Exception as e:
            log.debug("post enrich failed %s: %s", post_url, e)
        return raw

    @staticmethod
    async def _posts_from_raw(query: str, raw: dict[str, Any], cap: int) -> list[JobRecord]:
        text = (raw.get("text") or "").strip()
        if len(text) < 40 or not looks_like_hiring(text):
            return []

        author = (raw.get("author") or "").strip()
        urn = raw.get("urn") or ""
        post_url = raw.get("postUrl") or (
            f"https://www.linkedin.com/feed/update/{urn}/" if urn.startswith("urn:") else None
        )
        source_id = urn or hashlib.sha1(text[:200].encode("utf-8")).hexdigest()
        posted_at = parse_relative_posted_at(f"{raw.get('postedHint') or ''} {raw.get('subtitle') or ''}")

        settings = get_settings()
        if settings.scraper_llm_enrich:
            llm_recs = await llm_records_from_text(
                text,
                source="scrape:linkedin_posts",
                source_id_prefix=source_id,
                context="linkedin_hiring_post",
                url=post_url,
                author=author,
                fallback_query=query,
            )
            if llm_recs:
                out: list[JobRecord] = []
                for rec in llm_recs[:cap]:
                    out.append(
                        JobRecord(
                            source=rec.source,
                            source_id=rec.source_id,
                            company=rec.company,
                            role=rec.role,
                            jd_text=rec.jd_text,
                            jd_url=rec.jd_url or post_url,
                            location=rec.location or extract_location(text),
                            remote=rec.remote or ("remote" in text.lower()),
                            recruiter_name=rec.recruiter_name or author or None,
                            hr_email=rec.hr_email,
                            posted_at=posted_at,
                            raw_payload={
                                **rec.raw_payload,
                                "query": query,
                                "urn": urn,
                                "extractor": "llm_primary",
                            },
                        )
                    )
                return out

        rec = LinkedInPostsScraper._post_to_record(query, raw, cap)
        if rec is None:
            return []
        if settings.scraper_llm_enrich:
            rec = await llm_enrich_record(
                rec, context="linkedin_hiring_post", author=author
            )
        return [rec]

    @staticmethod
    def _post_to_record(query: str, raw: dict[str, Any], cap: int) -> JobRecord | None:
        text = (raw.get("text") or "").strip()
        if len(text) < 40 or not looks_like_hiring(text):
            return None

        email = raw.get("page_emails") or extract_email(text)
        author = (raw.get("author") or "").strip()
        subtitle = (raw.get("subtitle") or "").strip()
        company = extract_company(subtitle, text) or "Unknown"
        role = extract_role(text, fallback=query.split('"')[1] if '"' in query else query)
        urn = raw.get("urn") or ""
        source_id = urn or hashlib.sha1(text[:200].encode("utf-8")).hexdigest()
        post_url = raw.get("postUrl") or (
            f"https://www.linkedin.com/feed/update/{urn}/" if urn.startswith("urn:") else None
        )
        posted_at = parse_relative_posted_at(
            f"{raw.get('postedHint') or ''} {subtitle}"
        )

        return JobRecord(
            source="scrape:linkedin_posts",
            source_id=source_id,
            company=company,
            role=role,
            jd_text=text[:4000],
            jd_url=post_url,
            location=extract_location(text),
            remote="remote" in text.lower(),
            recruiter_name=author or None,
            hr_email=email,
            posted_at=posted_at,
            raw_payload={"query": query, "subtitle": subtitle, "urn": urn},
        )


# Backward-compatible alias
LinkedInPostsSource = LinkedInPostsScraper
