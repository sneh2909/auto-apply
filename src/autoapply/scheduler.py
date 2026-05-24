"""APScheduler entry point.

Run with:
  python -m autoapply.scheduler
"""

from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from autoapply.channel.email.composer import compose_email
from autoapply.channel.email.hr_resolver import resolve_hr_email
from autoapply.channel.email.sender import send_application
from autoapply.channel.router import channel_letter_to_enum, decide
from autoapply.config import get_companies, get_settings
from autoapply.db import close_db, get_db, init_db
from autoapply.db.models import Application, AppStatus, Channel, Job
from autoapply.score.scorer import score_unscored_jobs
from autoapply.sources.ats.greenhouse import GreenhouseSource
from autoapply.sources.ats.lever import LeverSource
from autoapply.sources.base import Source, upsert_jobs
from autoapply.sources.gmail_alerts import GmailAlertSource
from autoapply.sources.parsers import all_parsers
from autoapply.sources.scrapers.foundit import FounditScraper
from autoapply.sources.scrapers.instahyre import InstahyreScraper
from autoapply.sources.scrapers.linkedin import LinkedInScraper
from autoapply.sources.scrapers.naukri import NaukriScraper
from autoapply.tracker.reply_watcher import check_replies

log = logging.getLogger(__name__)


async def ingest_once() -> None:
    """Run every source once and persist — all sources fire in parallel."""
    log.info("─── ingest started ───")
    settings = get_settings()
    gmail_src = GmailAlertSource(all_parsers())
    ats_sources: list[Source] = []
    if settings.ingest_ats_sources:
        companies = get_companies()
        if companies.greenhouse or companies.lever:
            if companies.greenhouse:
                ats_sources.append(GreenhouseSource())
            if companies.lever:
                ats_sources.append(LeverSource())
        else:
            log.info("ingest_ats_sources enabled but config/companies.yaml has no slugs — skipping ATS")
    enabled = set(settings.scraper_portals)
    _all_scrapers: list[Source] = [
        LinkedInScraper(),
        NaukriScraper(),
        FounditScraper(),
        InstahyreScraper(),
    ]
    scrape_sources: list[Source] = [
        s for s in _all_scrapers
        if any(p in s.name for p in enabled)
    ]

    async def _run_scraper(src: Source) -> None:
        try:
            records = await src.fetch()
            log.info("%s → %d records emitted", src.name, len(records))
        except Exception:
            log.exception("scrape source %s failed", src.name)

    async def _run_gmail() -> None:
        try:
            await gmail_src.fetch()
            log.info("gmail alerts → done")
        except RuntimeError as e:
            log.warning("gmail ingest skipped: %s", e)

    async def _run_ats(src: Source) -> None:
        try:
            records = await src.fetch()
            inserted, skipped = await upsert_jobs(records)
            log.info("%s → inserted=%d skipped=%d", src.name, inserted, skipped)
        except Exception:
            log.exception("ats source %s failed", src.name)

    await asyncio.gather(
        *[_run_scraper(src) for src in scrape_sources],
        _run_gmail(),
        *[_run_ats(src) for src in ats_sources],
    )
    log.info("─── ingest done ───")


async def score_once(limit: int = 100) -> int:
    n = await score_unscored_jobs(limit=limit)
    log.info("scored %d jobs", n)
    return n


async def route_once(limit: int = 50) -> int:
    """For each scored Job without an Application, decide a channel and create the Application row."""
    settings = get_settings()
    db = get_db()
    job_coll = db[Job.collection]
    app_coll = db[Application.collection]

    cursor = job_coll.find({"fit_score": {"$ne": None}}).sort("fit_score", -1).limit(limit)
    routed = 0
    async for raw in cursor:
        job = Job.model_validate(raw)
        # Skip if any application already exists for this job.
        existing = await app_coll.find_one({"job_id": str(job.id)})
        if existing:
            continue

        letter, reason = await decide(job)
        if letter == "skip":
            log.info("skip %s @ %s (%s)", job.role, job.company, reason)
            continue
        enum_ch = channel_letter_to_enum(letter)
        if enum_ch is None:
            continue

        recipient = job.hr_email if letter == "B" else None
        if letter == "C":
            resolved = await resolve_hr_email(job)
            if resolved is None:
                enum_ch = channel_letter_to_enum("D")
                if enum_ch is None:
                    continue
                log.info("route %s @ %s to agent fallback (no HR email)", job.role, job.company)
            else:
                recipient = resolved.email
                await job_coll.update_one(
                    {"_id": raw["_id"]},
                    {"$set": {"hr_email": resolved.email}},
                )

        # High-score email jobs: compose + send immediately, bypass review queue.
        is_email_ch = enum_ch in (Channel.email_reply, Channel.coldmail)
        auto = (
            is_email_ch
            and recipient
            and job.fit_score is not None
            and job.fit_score >= settings.auto_apply_threshold
        )

        if auto:
            try:
                composed = await compose_email(job, recruiter_name=job.recruiter_name)
                app = Application(
                    job_id=str(job.id),
                    channel=enum_ch,
                    status=AppStatus.approved,
                    recipient=recipient,
                    draft_subject=composed.subject,
                    draft_body=composed.body,
                )
                await app.insert()
                await send_application(app)
                log.info(
                    "auto-applied %s @ %s (score=%.1f, email=%s)",
                    job.role, job.company, job.fit_score, recipient,
                )
            except Exception:
                log.exception("auto-apply failed for %s @ %s", job.role, job.company)
        else:
            app = Application(
                job_id=str(job.id),
                channel=enum_ch,
                status=AppStatus.queued,
                recipient=recipient,
                ats_url=job.jd_url if letter == "A" else None,
            )
            await app.insert()

        routed += 1
    log.info("routed %d new applications", routed)
    return routed


async def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    await init_db()

    sched = AsyncIOScheduler(timezone=get_settings().timezone)
    sched.add_job(ingest_once,    "interval", minutes=30, id="ingest",   max_instances=1)
    sched.add_job(score_once,     "interval", minutes=60, id="score",    max_instances=1)
    sched.add_job(route_once,     "interval", minutes=60, id="route",    max_instances=1)
    sched.add_job(check_replies,  "interval", minutes=10, id="replies",  max_instances=1)
    sched.start()
    log.info("scheduler started")

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    sched.shutdown(wait=False)
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
