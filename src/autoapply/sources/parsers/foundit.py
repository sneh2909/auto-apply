"""Parser for Foundit (formerly Monster India) job-alert emails. STUB."""

from __future__ import annotations

import logging
from typing import Any

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)


class FounditParser(EmailParser):
    name = "foundit"
    sender_match = "foundit.in"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        log.warning("FounditParser is a stub; received message %s", message.get("id"))
        return []
