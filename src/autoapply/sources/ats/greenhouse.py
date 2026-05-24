"""Greenhouse public job-board API ingestion.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
No auth, no rate-limit headers worth worrying about at our volume.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import datetime

import httpx
from selectolax.parser import HTMLParser

from autoapply.config import get_companies
from autoapply.sources.base import JobRecord, Source

log = logging.getLogger(__name__)

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return HTMLParser(s).text(separator=" ", strip=True)


class GreenhouseSource(Source):
    name = "ats:greenhouse"

    async def fetch(self) -> list[JobRecord]:
        companies = get_companies().greenhouse
        if not companies:
            return []

        records: list[JobRecord] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for c in companies:
                try:
                    resp = await client.get(API_URL.format(slug=c.slug))
                    if resp.status_code != 200:
                        log.warning("greenhouse %s returned %s", c.slug, resp.status_code)
                        continue
                    for j in resp.json().get("jobs", []):
                        records.append(self._to_record(c.label, j))
                except Exception as e:
                    log.exception("greenhouse fetch failed for %s: %s", c.slug, e)
        return records

    @staticmethod
    def _to_record(company_label: str, j: dict) -> JobRecord:
        loc = (j.get("location") or {}).get("name", "")
        jd = _strip_html(j.get("content", ""))
        updated = j.get("updated_at")
        posted_at = None
        if updated:
            with suppress(ValueError):
                posted_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return JobRecord(
            source="ats:greenhouse",
            source_id=str(j["id"]),
            company=company_label,
            role=j.get("title", ""),
            jd_text=jd,
            jd_url=j.get("absolute_url"),
            location=loc or None,
            remote=bool(_REMOTE_RE.search(loc or "")),
            posted_at=posted_at,
            raw_payload={"greenhouse_id": j["id"]},
        )
