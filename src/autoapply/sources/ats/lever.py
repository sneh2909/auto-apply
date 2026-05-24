"""Lever public job-postings API ingestion.

Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import UTC, datetime

import httpx
from selectolax.parser import HTMLParser

from autoapply.config import get_companies
from autoapply.sources.base import JobRecord, Source

log = logging.getLogger(__name__)

API_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return HTMLParser(s).text(separator=" ", strip=True)


class LeverSource(Source):
    name = "ats:lever"

    async def fetch(self) -> list[JobRecord]:
        companies = get_companies().lever
        if not companies:
            return []

        records: list[JobRecord] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for c in companies:
                try:
                    resp = await client.get(API_URL.format(slug=c.slug))
                    if resp.status_code != 200:
                        log.warning("lever %s returned %s", c.slug, resp.status_code)
                        continue
                    for j in resp.json():
                        records.append(self._to_record(c.label, j))
                except Exception as e:
                    log.exception("lever fetch failed for %s: %s", c.slug, e)
        return records

    @staticmethod
    def _to_record(company_label: str, j: dict) -> JobRecord:
        categories = j.get("categories") or {}
        loc = categories.get("location", "")
        commitment = categories.get("commitment", "")
        team = categories.get("team", "")
        jd_text = _strip_html(j.get("descriptionPlain") or j.get("description", ""))
        created_at = None
        if j.get("createdAt"):
            with suppress(TypeError, ValueError):
                created_at = datetime.fromtimestamp(j["createdAt"] / 1000, UTC)

        return JobRecord(
            source="ats:lever",
            source_id=j["id"],
            company=company_label,
            role=j.get("text", ""),
            jd_text=jd_text,
            jd_url=j.get("hostedUrl"),
            location=loc or None,
            remote=bool(_REMOTE_RE.search(loc or "")),
            posted_at=created_at,
            raw_payload={"lever_id": j["id"], "team": team, "commitment": commitment},
        )
