"""Parser for Wellfound (AngelList) job-alert emails. STUB."""

from __future__ import annotations

import logging
from typing import Any

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)


class WellfoundParser(EmailParser):
    name = "wellfound"
    sender_match = "wellfound.com"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        log.warning("WellfoundParser is a stub; received message %s", message.get("id"))
        return []
