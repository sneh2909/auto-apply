"""Ashby public job-board API ingestion. STUB.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
Mirror Greenhouse/Lever once we have a target company on Ashby.
"""

from __future__ import annotations

import logging

from autoapply.sources.base import JobRecord, Source

log = logging.getLogger(__name__)


class AshbySource(Source):
    name = "ats:ashby"

    async def fetch(self) -> list[JobRecord]:
        log.info("AshbySource is a stub")
        return []
