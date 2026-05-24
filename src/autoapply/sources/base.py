"""Source contracts. Every ingestion path produces a list of `JobRecord` and persists via `upsert_jobs`."""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from autoapply.db import get_db
from autoapply.db.models import Job

log = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
}


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").lower()).strip()


def _url_key(url: str | None) -> str | None:
    """Normalize job URLs enough to catch the same link with common tracking noise."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.netloc:
        return url.strip().lower() or None
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
        and k.lower() not in _TRACKING_PARAMS
    ]
    normalized = parsed._replace(
        scheme=parsed.scheme.lower() or "https",
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
        params="",
        query=urlencode(sorted(query)),
        fragment="",
    )
    return urlunparse(normalized)


@dataclass
class JobRecord:
    """Source-neutral job representation. Becomes a `Job` document on persist."""

    source: str
    source_id: str
    company: str
    role: str
    jd_text: str
    jd_url: str | None = None
    location: str | None = None
    remote: bool = False
    posted_at: datetime | None = None
    recruiter_name: str | None = None
    hr_email: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_hash(self) -> str:
        """Stable across small JD changes; collides on (company, role, opening 200 chars)."""
        key = f"{_norm(self.company)}|{_norm(self.role)}|{_norm(self.jd_text)[:200]}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def to_job(self) -> Job:
        return Job(
            source=self.source,
            source_id=self.source_id,
            company=self.company.strip(),
            role=self.role.strip(),
            jd_text=self.jd_text,
            jd_url=self.jd_url,
            jd_url_key=_url_key(self.jd_url),
            location=self.location,
            remote=self.remote,
            posted_at=self.posted_at,
            recruiter_name=self.recruiter_name,
            hr_email=self.hr_email,
            raw_payload=self.raw_payload,
            dedup_hash=self.dedup_hash,
        )


class Source(ABC):
    name: str

    @abstractmethod
    async def fetch(self) -> list[JobRecord]:
        ...


class EmailParser(ABC):
    """Parses a single Gmail message (the dict returned by `users.messages.get`)."""

    name: str               # short id, used as part of Job.source
    sender_match: str       # case-insensitive substring of the From: header

    def matches(self, from_header: str) -> bool:
        return self.sender_match.lower() in (from_header or "").lower()

    def matches_message(
        self,
        message: dict[str, Any],
        from_header: str,
        subject: str,
        html: str,
        text: str,
    ) -> bool:
        """Override to match on subject/body (e.g. Jobs2Web talent-community digests)."""
        return self.matches(from_header)

    @abstractmethod
    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        ...


# === Persistence helper ====================================================


async def upsert_jobs(records: list[JobRecord]) -> tuple[int, int]:
    """Insert new jobs; skip duplicates by job link first, then content hash."""
    if not records:
        return 0, 0

    from autoapply.sources.job_filter import filter_job_records

    records = filter_job_records(records)
    if not records:
        return 0, 0

    coll = get_db()[Job.collection]
    inserted = 0
    skipped = 0
    for rec in records:
        job = rec.to_job()
        doc = job.model_dump(by_alias=True, exclude_none=True)
        doc.pop("_id", None)
        duplicate_checks: list[dict[str, Any]] = [{"dedup_hash": job.dedup_hash}]
        if job.jd_url_key:
            duplicate_checks.append({"jd_url_key": job.jd_url_key})
        if job.jd_url:
            duplicate_checks.append({"jd_url": job.jd_url})
        filt = duplicate_checks[0] if len(duplicate_checks) == 1 else {"$or": duplicate_checks}

        # MongoDB error code 40: a path cannot appear in both $set and $setOnInsert.
        # Solution: exclude jd_url_key from $setOnInsert; $set covers both insert and update,
        # which also backfills the field on any pre-existing document that lacks it.
        set_fields: dict[str, Any] = {}
        if job.jd_url_key:
            set_fields["jd_url_key"] = job.jd_url_key
        insert_doc = {k: v for k, v in doc.items() if k not in set_fields}
        update: dict[str, Any] = {"$setOnInsert": insert_doc}
        if set_fields:
            update["$set"] = set_fields

        try:
            result = await coll.update_one(
                filt,
                update,
                upsert=True,
            )
            if result.upserted_id is not None:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            log.exception("upsert failed for %s @ %s: %s", rec.role, rec.company, e)
    return inserted, skipped
