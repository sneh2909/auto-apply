"""LLM extraction for scraped web pages and hiring posts.

Uses the agent LLM to pull structured job data and recruiter emails from
arbitrary page text — including obfuscated addresses (name [at] company dot com).
Regex/DOM extraction runs first; LLM refines or replaces fields for best quality.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from autoapply.score.llm import get_agent_llm
from autoapply.sources.base import JobRecord
from autoapply.sources.scrape_utils import extract_email, looks_like_hiring

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PERSONAL_BLOCKED = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "notifications",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "jobs2web",
    "google.com",
)


def looks_personal_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    local, _, domain = email.lower().partition("@")
    if any(b in local for b in _PERSONAL_BLOCKED):
        return False
    if any(b in domain for b in _PERSONAL_BLOCKED):
        return False
    return True


def normalize_obfuscated_email(text: str) -> str | None:
    """Decode 'name [at] company [dot] com' style obfuscation."""
    patterns = (
        r"([A-Za-z0-9._%+\-]+)\s*\[\s*at\s*\]\s*([A-Za-z0-9.\-]+)\s*\[\s*dot\s*\]\s*([A-Za-z]{2,})",
        r"([A-Za-z0-9._%+\-]+)\s*@\s*([A-Za-z0-9.\-]+)\s*dot\s*([A-Za-z]{2,})",
        r"([A-Za-z0-9._%+\-]+)\s*\(\s*at\s*\)\s*([A-Za-z0-9.\-]+)\s*\(\s*dot\s*\)\s*([A-Za-z]{2,})",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return f"{m.group(1)}@{m.group(2)}.{m.group(3)}"
    return None


_PAGE_PROMPT = """\
You extract job openings from web page text (LinkedIn hiring posts, job boards, \
career pages, Google Jobs snippets, recruiter announcements).

Return strict JSON only — no markdown, no prose:
{{
  "jobs": [
    {{
      "company": "Acme AI",
      "role": "Senior Machine Learning Engineer",
      "location": "Bangalore" or null,
      "remote": false,
      "jd_url": "https://..." or null,
      "hr_email": "recruiter@acme.com" or null,
      "recruiter_name": "Priya Sharma" or null,
      "jd_summary": "2-4 sentences describing the role, requirements, and how to apply",
      "confidence": 0.95
    }}
  ]
}}

EMAIL RULES (critical):
- Extract ANY personal recruiter / hiring-manager email, including obfuscated forms:
  "priya [at] acme [dot] ai", "email me at name(at)company.com", "DM or mail hr@..."
- NEVER return noreply, notifications, linkedin.com, jobs2web, or platform emails.
- Prefer the email the poster asks candidates to contact.
- If the post says "DM me" with no email, set hr_email to null.

JOB RULES:
- One entry per distinct role mentioned (hiring posts may list multiple openings).
- role = exact job title (not "we're hiring").
- company = employer name (not a staffing agency unless that's the employer).
- jd_summary should mention key skills, YOE, and apply instructions if present.
- confidence 0.0–1.0 for how sure you are this is a real job posting.

If nothing job-related: {{"jobs": []}}

SOURCE CONTEXT: {context}
PAGE URL: {url}
AUTHOR / POSTER: {author}

PAGE TEXT:
{text}
"""


async def extract_jobs_from_text(
    text: str,
    *,
    context: str = "web_scrape",
    url: str | None = None,
    author: str | None = None,
    max_chars: int = 8000,
) -> list[dict[str, Any]]:
    """LLM → list of job dicts with company, role, hr_email, etc."""
    body = (text or "").strip()
    if len(body) < 30:
        return []

    # Skip obvious non-hiring pages unless LLM might still find embedded jobs
    if len(body) > 200 and not looks_like_hiring(body) and "engineer" not in body.lower():
        if context not in ("linkedin_job_page", "browser_agent_fetch", "google_jobs"):
            return []

    prompt = _PAGE_PROMPT.format(
        context=context,
        url=url or "(unknown)",
        author=author or "(unknown)",
        text=body[:max_chars],
    )

    try:
        llm = get_agent_llm()
        out = await llm.chat_json(
            [
                {
                    "role": "system",
                    "content": "Return strict JSON only. Decode obfuscated emails to standard format.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1200,
        )
    except Exception:
        log.exception("LLM job extract failed (context=%s url=%s)", context, url)
        return []

    jobs: list[dict[str, Any]] = []
    for raw in (out or {}).get("jobs") or []:
        if not isinstance(raw, dict):
            continue
        company = (raw.get("company") or "").strip()
        role = (raw.get("role") or "").strip()
        if not role or len(role) < 3:
            continue
        hr_email = (raw.get("hr_email") or "").strip()
        if hr_email and not looks_personal_email(hr_email):
            hr_email = ""
        if not hr_email:
            ob = normalize_obfuscated_email(body)
            if ob and looks_personal_email(ob):
                hr_email = ob
        if not hr_email:
            for found in _EMAIL_RE.findall(body):
                if looks_personal_email(found):
                    hr_email = found
                    break
        raw["hr_email"] = hr_email or None
        raw["company"] = company or "Unknown"
        raw["role"] = role
        jobs.append(raw)

    if jobs:
        log.info(
            "LLM extracted %d job(s) from %s (emails=%d)",
            len(jobs),
            context,
            sum(1 for j in jobs if j.get("hr_email")),
        )
    return jobs


def _merge_llm_into_record(record: JobRecord, llm: dict[str, Any]) -> JobRecord:
    """Prefer LLM fields when confidence >= 0.5 or when regex missed email."""
    conf = float(llm.get("confidence") or 0.7)
    llm_email = llm.get("hr_email")
    use_llm = conf >= 0.5

    company = llm.get("company") if use_llm and llm.get("company") else record.company
    role = llm.get("role") if use_llm and llm.get("role") else record.role
    location = llm.get("location") if use_llm and llm.get("location") else record.location
    jd_url = llm.get("jd_url") if use_llm and llm.get("jd_url") else record.jd_url
    summary = (llm.get("jd_summary") or "").strip()
    jd_text = summary if summary and use_llm else record.jd_text
    recruiter = llm.get("recruiter_name") or record.recruiter_name

    hr_email = record.hr_email
    if llm_email and looks_personal_email(llm_email):
        hr_email = llm_email  # LLM email always wins when valid
    elif not hr_email and llm_email:
        hr_email = llm_email if looks_personal_email(llm_email) else hr_email

    remote = record.remote
    if llm.get("remote") is True:
        remote = True

    payload = {**record.raw_payload, "llm_enriched": True, "llm_confidence": conf}
    return JobRecord(
        source=record.source,
        source_id=record.source_id,
        company=company or record.company,
        role=role or record.role,
        jd_text=jd_text,
        jd_url=jd_url,
        location=location,
        remote=remote,
        posted_at=record.posted_at,
        recruiter_name=recruiter,
        hr_email=hr_email,
        raw_payload=payload,
    )


async def llm_enrich_record(
    record: JobRecord,
    *,
    context: str = "web_scrape",
    author: str | None = None,
) -> JobRecord:
    """Run LLM on a JobRecord's jd_text; merge best fields back."""
    from dataclasses import replace

    from autoapply.config import get_settings

    if not get_settings().scraper_llm_enrich:
        return record

    jobs = await extract_jobs_from_text(
        record.jd_text,
        context=context,
        url=record.jd_url,
        author=author or record.recruiter_name,
    )
    if not jobs:
        ob = normalize_obfuscated_email(record.jd_text)
        if ob and looks_personal_email(ob) and not record.hr_email:
            return replace(
                record,
                hr_email=ob,
                raw_payload={**record.raw_payload, "email_source": "obfuscated_regex"},
            )
        return record

    best = max(jobs, key=lambda j: float(j.get("confidence") or 0))
    return _merge_llm_into_record(record, best)


async def llm_records_from_text(
    text: str,
    *,
    source: str,
    source_id_prefix: str,
    context: str,
    url: str | None = None,
    author: str | None = None,
    fallback_query: str = "",
) -> list[JobRecord]:
    """Create JobRecords purely from LLM extraction (e.g. hiring posts)."""
    from autoapply.config import get_settings

    if not get_settings().scraper_llm_enrich:
        return []

    jobs = await extract_jobs_from_text(text, context=context, url=url, author=author)
    records: list[JobRecord] = []
    for i, j in enumerate(jobs):
        sid = f"{source_id_prefix}-{i}" if len(jobs) > 1 else source_id_prefix
        hr = j.get("hr_email")
        records.append(
            JobRecord(
                source=source,
                source_id=sid,
                company=j.get("company") or "Unknown",
                role=j.get("role") or fallback_query,
                jd_text=(j.get("jd_summary") or text)[:4000],
                jd_url=j.get("jd_url") or url,
                location=j.get("location"),
                remote=bool(j.get("remote")),
                recruiter_name=j.get("recruiter_name") or author,
                hr_email=hr if looks_personal_email(hr) else None,
                raw_payload={"extractor": "llm", "llm_confidence": j.get("confidence")},
            )
        )
    return records
