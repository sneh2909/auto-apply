"""Parser for Hirect job-alert emails. STUB."""

from __future__ import annotations

import logging
from typing import Any

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)


class HirectParser(EmailParser):
    name = "hirect"
    sender_match = "hirect"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        log.warning("HirectParser is a stub; received message %s", message.get("id"))
        return []
