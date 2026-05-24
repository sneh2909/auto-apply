"""Pick the application channel (A/B/C/D) for a given Job.

Order of preference:
  A — `jd_url` is a known ATS (greenhouse/lever/ashby/naukri easy-apply).
  B — Job carries an `hr_email` already (the alert parser extracted one).
  C — `hr_resolver` cascade produces an email for this company.
  D — fallback for long-tail sites that none of the above handle.
  skip — fit-score below threshold, blocked company, or duplicate.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

from autoapply.config import get_blocklist, get_settings
from autoapply.db import get_db
from autoapply.db.models import Application, AppStatus, Channel, Job

log = logging.getLogger(__name__)

Decision = Literal["A", "B", "C", "D", "skip"]

_ATS_HOST_PATTERNS = {
    "A:greenhouse": re.compile(r"(boards\.greenhouse\.io|greenhouse\.io/embed|grnh\.se)", re.IGNORECASE),
    "A:lever":      re.compile(r"jobs\.lever\.co", re.IGNORECASE),
    "A:ashby":      re.compile(r"jobs\.ashbyhq\.com", re.IGNORECASE),
    "A:workday":    re.compile(r"(myworkdayjobs\.com|workday\.com)", re.IGNORECASE),
    "A:naukri":     re.compile(r"naukri\.com", re.IGNORECASE),
    "A:foundit":    re.compile(r"foundit\.in", re.IGNORECASE),
    "A:instahyre":  re.compile(r"instahyre\.com", re.IGNORECASE),
}


def _detect_ats(jd_url: str | None) -> str | None:
    if not jd_url:
        return None
    host = urlparse(jd_url).netloc + urlparse(jd_url).path
    for label, pat in _ATS_HOST_PATTERNS.items():
        if pat.search(host):
            return label
    return None


def _is_blocked(job: Job) -> bool:
    bl = get_blocklist()
    company_lc = job.company.lower()
    if any(c.lower() in company_lc for c in bl.companies):
        return True
    if job.jd_url:
        host = urlparse(job.jd_url).netloc.lower()
        if any(d.lower() in host for d in bl.domains):
            return True
    return False


async def _already_applied(job: Job) -> bool:
    """Same company + role within the cooldown window."""
    cooldown = get_settings().cooldown_days
    cutoff = datetime.now(UTC) - timedelta(days=cooldown)
    coll = get_db()[Application.collection]
    # join via job_id is heavy; cheaper: query jobs collection for same company+role recently applied.
    job_ids_cursor = get_db()[Job.collection].find(
        {"company": job.company, "role": {"$regex": f"^{re.escape(job.role[:20])}", "$options": "i"}},
        {"_id": 1},
    )
    job_ids: list[str] = []
    async for d in job_ids_cursor:
        job_ids.append(str(d["_id"]))
    if not job_ids:
        return False
    existing = await coll.find_one(
        {
            "job_id": {"$in": job_ids},
            "status": {"$in": [AppStatus.sent.value, AppStatus.submitted.value]},
            "sent_at": {"$gte": cutoff},
        }
    )
    return existing is not None


async def decide(job: Job) -> tuple[Decision, str]:
    """Return (channel_letter, reason)."""
    settings = get_settings()

    if job.fit_score is None:
        return "skip", "not scored yet"
    if job.fit_score < settings.fit_threshold:
        return "skip", f"fit {job.fit_score} < threshold {settings.fit_threshold}"
    if job.deal_breakers:
        return "skip", f"deal-breaker: {job.deal_breakers[0]}"
    if _is_blocked(job):
        return "skip", "blocked company/domain"
    if await _already_applied(job):
        return "skip", "already applied within cooldown"

    if (ats := _detect_ats(job.jd_url)) is not None:
        return "A", ats
    if job.hr_email:
        return "B", "hr_email present from source"
    # Channel C and D require active resolution; queue them via status, decision is made at apply-time.
    return "C", "cold email — needs hr_resolver run"


def channel_letter_to_enum(letter: Decision) -> Channel | None:
    return {
        "A": Channel.ats,
        "B": Channel.email_reply,
        "C": Channel.coldmail,
        "D": Channel.agent,
    }.get(letter)
