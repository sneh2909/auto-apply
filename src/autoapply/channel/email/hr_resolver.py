"""Resolve a recruiter/HR email for a Job via a free-tools-only cascade.

Order:
  1. Email already present on the Job (extracted by the source parser/scraper).
  2. Regex on the JD text (catches alerts that mention `recruiter@company.com` mid-body).
  3. Scrape the company careers / about / contact page.
  4. Fallback `careers@<domain>` (low yield but better than skipping).

No paid services. If none of the above produces a hit, return None and the
router falls through to Channel D / skip per `channel.router.decide`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

from autoapply.db.models import Job

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BAD_SUFFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster")
_BAD_DOMAINS = ("naukri.com", "linkedin.com", "instahyre.com", "indeed.com", "hirect", "wellfound.com", "monster.com", "foundit.in")


@dataclass(slots=True)
class ResolvedEmail:
    email: str
    source: str   # "alert" | "jd_regex" | "careers_page" | "fallback"


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _looks_personal(email: str) -> bool:
    """Reject role inboxes (noreply, mailer-daemon) and platform-owned addresses."""
    lc = email.lower()
    if any(b in lc for b in _BAD_SUFFIXES):
        return False
    return not any(d in lc for d in _BAD_DOMAINS)


def _filter_emails(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for e in candidates:
        if e in seen or not _looks_personal(e):
            continue
        seen.add(e)
        out.append(e)
    return out


async def _from_existing(job: Job) -> ResolvedEmail | None:
    if job.hr_email and _looks_personal(job.hr_email):
        return ResolvedEmail(email=job.hr_email, source="alert")
    return None


async def _from_jd_regex(job: Job) -> ResolvedEmail | None:
    found = _filter_emails(_EMAIL_RE.findall(job.jd_text or ""))
    if found:
        return ResolvedEmail(email=found[0], source="jd_regex")
    return None


async def _from_careers_page(job: Job) -> ResolvedEmail | None:
    domain = _domain_from_url(job.jd_url)
    if not domain or any(b in domain for b in _BAD_DOMAINS):
        return None
    candidates_urls = [
        f"https://{domain}/careers",
        f"https://{domain}/jobs",
        f"https://{domain}/careers/contact",
        f"https://{domain}/contact",
        f"https://{domain}/about",
    ]
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
        for url in candidates_urls:
            try:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (autoapply-bot/0.1)"})
                if r.status_code != 200:
                    continue
                text = HTMLParser(r.text).text(separator=" ", strip=True)
                found = _filter_emails(_EMAIL_RE.findall(text))
                if found:
                    return ResolvedEmail(email=found[0], source="careers_page")
            except Exception as e:
                log.debug("careers fetch %s failed: %s", url, e)
    return None


async def _fallback_role_inbox(job: Job) -> ResolvedEmail | None:
    domain = _domain_from_url(job.jd_url)
    if not domain or any(b in domain for b in _BAD_DOMAINS):
        return None
    return ResolvedEmail(email=f"careers@{domain}", source="fallback")


async def resolve_hr_email(job: Job) -> ResolvedEmail | None:
    for step in (_from_existing, _from_jd_regex, _from_careers_page, _fallback_role_inbox):
        try:
            result = await step(job)
        except Exception as e:
            log.exception("resolver step %s failed: %s", step.__name__, e)
            continue
        if result is not None:
            log.info("resolved %s for %s via %s", result.email, job.company, result.source)
            return result
    return None
