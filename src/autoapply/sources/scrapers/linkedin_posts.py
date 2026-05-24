"""Scrape LinkedIn hiring posts via your logged-in browser session.

The pattern: recruiters post in their feed like

    "We're hiring a Senior ML Engineer at Acme Corp! 4-7 yrs, Bangalore.
     DM me or email priya@acme.com — would love to chat."

These posts are the single highest-yield source of HR emails in the Indian ML
market because the email is *intentionally published* by the recruiter — no
guessing, no enrichment service required.

Strategy:
  1. Use Playwright with a persistent user-data-dir holding your LinkedIn cookies
     (bootstrap via `scripts/setup_linkedin_login.py`).
  2. For each search query in settings.linkedin_search_queries, hit the content
     search URL and scroll N times.
  3. Extract each post card via a JS one-liner (DOM selectors centralised inside
     `page.evaluate` — easier to update when LinkedIn changes class names).
  4. Filter to "looks like a hiring post" by keyword heuristic.
  5. Pull email via regex, company from author headline, role from text patterns.
  6. Emit one JobRecord per qualifying post; persist via the standard upsert.

LinkedIn DOM is unstable. Every selector here has at least one fallback. When the
scraper's emit-rate drops noticeably, check `SourceRun.parse_errors` and tweak
the JS extractor. There are no tests for this file because it can only be
verified against a real authenticated LinkedIn session.
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

from autoapply.config import get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import JobRecord, Source, upsert_jobs

log = logging.getLogger(__name__)

# === Heuristics ===========================================================

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Words that, if any of them appear, mark a post as a hiring post.
_HIRING_TOKENS = (
    "hiring", "we're hiring", "we are hiring", "open role", "open position",
    "looking for", "looking to hire", "join our team", "join us", "join the team",
    "now hiring", "actively hiring", "currently hiring",
    "send your resume", "send your cv", "share your profile", "share your cv",
    "dm me", "drop a dm", "reach out", "apply now",
)

# Role hint patterns — we capture group(1) as the role title.
_ROLE_PATTERNS = (
    re.compile(r"hiring (?:a |an |for a |for an )?([A-Z][\w\s/+\-]{2,60}?)\s*(?:[,.\n]|at )", re.IGNORECASE),
    re.compile(r"looking for (?:a |an )?([A-Z][\w\s/+\-]{2,60}?)\s*(?:[,.\n]|to )", re.IGNORECASE),
    re.compile(r"\bopen (?:role|position)\s*(?:for|:)\s*([A-Z][\w\s/+\-]{2,60}?)\s*(?:[,.\n]|at )", re.IGNORECASE),
    re.compile(r"\brole:\s*([A-Z][\w\s/+\-]{2,60}?)\s*(?:[,.\n])", re.IGNORECASE),
    re.compile(r"\bposition:\s*([A-Z][\w\s/+\-]{2,60}?)\s*(?:[,.\n])", re.IGNORECASE),
)

_INDIA_CITIES = (
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "ncr",
    "gurgaon", "gurugram", "noida", "chennai", "kolkata", "ahmedabad",
)

_COMPANY_AT_RE = re.compile(r"\bat\s+([A-Z][\w&.\- ]{1,40}?)(?:\s*(?:\(|\||·|\.|,|$))")


def _looks_like_hiring(text: str) -> bool:
    lc = text.lower()
    return any(t in lc for t in _HIRING_TOKENS)


def _extract_email(text: str) -> str | None:
    for m in _EMAIL_RE.findall(text):
        lc = m.lower()
        if "linkedin" in lc or "noreply" in lc or "no-reply" in lc:
            continue
        return m
    return None


def _extract_role(text: str) -> str | None:
    for pat in _ROLE_PATTERNS:
        m = pat.search(text)
        if m:
            role = re.sub(r"\s+", " ", m.group(1)).strip()
            # Trim trailing prepositions that get caught.
            role = re.sub(r"\s+(in|at|for|with|to)\s*$", "", role, flags=re.IGNORECASE).strip()
            if 4 <= len(role) <= 80:
                return role
    return None


def _extract_company(subtitle: str, text: str) -> str | None:
    """Try the author's subtitle first ('Talent @ Acme · 1st'), then 'at Acme' in body."""
    for chunk in (subtitle, text):
        if not chunk:
            continue
        for marker in (" @ ", " at "):
            if marker in chunk:
                tail = chunk.split(marker, 1)[1]
                cand = re.split(r"[·|()|·,.\n]", tail, maxsplit=1)[0].strip()
                if 2 <= len(cand) <= 60:
                    return cand
        m = _COMPANY_AT_RE.search(chunk)
        if m:
            return m.group(1).strip()
    return None


def _extract_location(text: str) -> str | None:
    lc = text.lower()
    for city in _INDIA_CITIES:
        if city in lc:
            return city.title() if city != "ncr" else "NCR"
    if "remote" in lc:
        return "Remote"
    return None


# === Page-side extractor ==================================================
#
# Centralised here so when LinkedIn renames classes we update one string. Returns
# a list of { urn, author, subtitle, text, postUrl } dicts.

_EXTRACTOR_JS = r"""
() => {
  const sel = (root, sels) => {
    for (const s of sels) {
      const el = root.querySelector(s);
      if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
    }
    return '';
  };
  const cards = document.querySelectorAll('[data-urn^="urn:li:activity:"], div.feed-shared-update-v2');
  const out = [];
  for (const card of cards) {
    const urn = card.getAttribute('data-urn') || (card.id || '');
    const author = sel(card, [
      '.update-components-actor__title span[aria-hidden="true"]',
      '.update-components-actor__title',
      '.feed-shared-actor__title',
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
    ]);
    if (!text || text.length < 50) continue;
    out.push({ urn, author, subtitle, text });
    if (out.length >= 40) break;
  }
  return out;
}
"""


# === The source ===========================================================


class LinkedInPostsSource(Source):
    name = "scrape:linkedin_posts"

    async def fetch(self) -> list[JobRecord]:
        from playwright.async_api import async_playwright

        settings = get_settings()
        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()

        records: list[JobRecord] = []
        seen_urns: set[str] = set()

        try:
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

                if not await self._is_logged_in(page):
                    run.notes = "not logged in — run scripts/setup_linkedin_login.py"
                    log.error(run.notes)
                else:
                    for query in settings.linkedin_search_queries:
                        try:
                            posts = await self._scrape_query(page, query, settings)
                            run.emails_seen += len(posts)
                            for raw in posts:
                                urn = raw.get("urn") or ""
                                if urn and urn in seen_urns:
                                    continue
                                seen_urns.add(urn)
                                rec = self._post_to_record(query, raw, settings.linkedin_max_posts_per_query)
                                if rec is not None:
                                    records.append(rec)
                                    run.jobs_emitted += 1
                        except Exception as e:
                            run.parse_errors += 1
                            log.exception("linkedin query %r failed: %s", query, e)
                        await asyncio.sleep(random.uniform(2, 5))  # be polite between queries

                await ctx.close()
        finally:
            inserted, skipped = await upsert_jobs(records)
            run.finished_at = datetime.now(UTC)
            run.notes = (run.notes + " | " if run.notes else "") + f"inserted={inserted} skipped={skipped}"
            await run.save()

        return records

    @staticmethod
    async def _is_logged_in(page) -> bool:
        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            url = page.url
            # Redirect to /login, /checkpoint, or bare homepage → not logged in
            if (
                "/login" in url
                or "/checkpoint" in url
                or url.rstrip("/") == "https://www.linkedin.com"
                or "authwall" in url
            ):
                return False
            # URL stayed on /feed/ → logged in (selector-free — LI changes class names often)
            return "/feed" in url
        except Exception:
            return False

    @staticmethod
    async def _scrape_query(page, query: str, settings) -> list[dict[str, Any]]:
        url = (
            "https://www.linkedin.com/search/results/content/"
            f"?keywords={quote_plus(query)}&datePosted=%22past-week%22&origin=GLOBAL_SEARCH_HEADER"
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        for _ in range(settings.linkedin_scroll_pages):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
            except Exception:
                break
            await asyncio.sleep(random.uniform(1.2, 2.5))

        try:
            results = await page.evaluate(_EXTRACTOR_JS)
        except Exception as e:
            log.warning("DOM extractor failed for %s: %s", query, e)
            return []

        if not results:
            try:
                html = await page.content()
                slug = query[:30].replace(" ", "_")
                dump_path = Path(f"/tmp/linkedin_dump_{slug}.html")
                dump_path.write_text(html[:15000], encoding="utf-8")
                log.warning(
                    "DOM extractor returned 0 posts for %r — first 15k of HTML at %s",
                    query, dump_path,
                )
            except Exception:
                pass
        return results

    @staticmethod
    def _post_to_record(query: str, raw: dict[str, Any], cap: int) -> JobRecord | None:
        text = (raw.get("text") or "").strip()
        if len(text) < 50 or not _looks_like_hiring(text):
            return None

        email = _extract_email(text)
        author = (raw.get("author") or "").strip()
        subtitle = (raw.get("subtitle") or "").strip()
        company = _extract_company(subtitle, text) or "Unknown"
        role = _extract_role(text) or query
        urn = raw.get("urn") or ""
        source_id = urn or hashlib.sha1(text[:200].encode("utf-8")).hexdigest()
        post_url = f"https://www.linkedin.com/feed/update/{urn}/" if urn.startswith("urn:") else None

        return JobRecord(
            source="scrape:linkedin_posts",
            source_id=source_id,
            company=company,
            role=role,
            jd_text=text,
            jd_url=post_url,
            location=_extract_location(text),
            remote="remote" in text.lower(),
            recruiter_name=author or None,
            hr_email=email,
            raw_payload={"query": query, "subtitle": subtitle, "urn": urn},
        )
