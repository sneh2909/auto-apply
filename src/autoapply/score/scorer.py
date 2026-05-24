"""Score a Job against the user's profile. Output is persisted on the Job document."""

from __future__ import annotations

import json
import logging
from typing import Any

from bson import ObjectId

from autoapply.config import Profile, get_profile
from autoapply.db import get_db
from autoapply.db.models import Job
from autoapply.score.llm import get_scorer_llm

log = logging.getLogger(__name__)

_JD_MAX_CHARS = 6000


def _build_prompt(profile: Profile, job: Job) -> str:
    profile_blob = json.dumps(profile.for_prompt(), indent=2, ensure_ascii=False)
    jd = (job.jd_text or "")[:_JD_MAX_CHARS]
    return f"""\
You are a blunt hiring-fit screener for an ML engineer's job search.

CANDIDATE PROFILE (JSON):
{profile_blob}

JOB:
Company:  {job.company}
Role:     {job.role}
Location: {job.location or "unspecified"}
Remote:   {"yes" if job.remote else "no/unknown"}
Source:   {job.source}

JD:
{jd}

Score fit on 0-10:
  0-3 poor (skip), 4-6 weak, 7-8 good, 9-10 must-apply.

Weigh:
  - Role title vs target.roles
  - Skill overlap with skills.primary then skills.secondary
  - Location vs target.on_site_cities_ok plus target.remote_ok
  - Seniority vs target.seniority
  - Deal-breakers from target.deal_breakers (any triggered → cap score at 4)
  - Industries: bonus for target.industries_priority, penalty for target.industries_avoid

Return strict JSON, no prose, this exact shape:
{{
  "score": 7.5,
  "reasons": ["one-line bullets, 3 to 5 of them"],
  "deal_breakers": ["only deal-breakers actually triggered; empty list if none"]
}}
"""


async def score_job(job: Job, profile: Profile | None = None) -> dict[str, Any]:
    """Returns {score, reasons, deal_breakers}. Clamps score to [0, 10]."""
    profile = profile or get_profile()

    if job.company.lower() in {c.lower() for c in profile.always_apply}:
        return {"score": 10.0, "reasons": ["company in profile.always_apply"], "deal_breakers": []}

    llm = get_scorer_llm()
    raw = await llm.chat_json(
        [
            {
                "role": "system",
                "content": "You return strict JSON only. No prose, no markdown fences.",
            },
            {"role": "user", "content": _build_prompt(profile, job)},
        ],
        temperature=0.1,
        max_tokens=500,
    )

    try:
        score = float(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0

    reasons = [str(r) for r in (raw.get("reasons") or [])][:6]
    deal_breakers = [str(d) for d in (raw.get("deal_breakers") or [])]

    score = max(0.0, min(10.0, score))
    if deal_breakers:
        score = min(score, 4.0)

    return {"score": score, "reasons": reasons, "deal_breakers": deal_breakers}


async def score_and_persist(job: Job) -> Job:
    result = await score_job(job)
    job.fit_score = result["score"]
    job.fit_reasons = result["reasons"]
    job.deal_breakers = result["deal_breakers"]

    if job.id:
        await get_db()[Job.collection].update_one(
            {"_id": ObjectId(job.id)},
            {"$set": {
                "fit_score": job.fit_score,
                "fit_reasons": job.fit_reasons,
                "deal_breakers": job.deal_breakers,
            }},
        )
    return job


async def score_unscored_jobs(limit: int = 100) -> int:
    """Score any jobs missing fit_score. Returns count processed."""
    db = get_db()
    cursor = db[Job.collection].find({"fit_score": None}).limit(limit)
    n = 0
    async for raw in cursor:
        job = Job.model_validate(raw)
        try:
            await score_and_persist(job)
            n += 1
        except Exception as e:
            log.exception("score failed for job %s @ %s: %s", job.role, job.company, e)
    return n
