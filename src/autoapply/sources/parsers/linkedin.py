"""Parser for LinkedIn job-alert emails.

LinkedIn sends "N new jobs for <role> in <city>" digest emails from
jobalerts-noreply@linkedin.com. Each email contains multiple job cards
with title (as a link), company, and location.

Setup (one-time on LinkedIn):
  Jobs → Job alerts → Create alert for each role + city → Weekly/Daily digest.
  LinkedIn emails arrive in your inbox → GmailAlertSource picks them up.

HR email yield: near zero (LinkedIn strips recruiter contacts from alert emails).
These jobs route to Channel A (ATS auto-fill) or Channel D (agent apply).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)

_JOB_URL_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs?/view/(\d+)",
    re.IGNORECASE,
)
_LOCATION_CLEANUP_RE = re.compile(r"\s*·\s*.+$")  # strip "· 2 days ago" tail


def _txt(node: Node | None) -> str:
    return node.text(strip=True, separator=" ").strip() if node else ""


def _clean_location(raw: str) -> str | None:
    cleaned = _LOCATION_CLEANUP_RE.sub("", raw).strip()
    return cleaned if cleaned else None


def _is_india_location(loc: str | None) -> bool:
    if not loc:
        return True  # unknown → don't filter
    india_signals = ("india", "bangalore", "bengaluru", "hyderabad", "pune",
                     "mumbai", "delhi", "ncr", "remote")
    return any(s in loc.lower() for s in india_signals)


class LinkedInParser(EmailParser):
    name = "linkedin"
    sender_match = "linkedin.com"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        if not html and not text:
            return []

        records: list[JobRecord] = []
        seen_ids: set[str] = set()

        if html:
            records = self._from_html(message, html, seen_ids)

        # Plaintext fallback (rare but some email clients strip HTML)
        if not records and text:
            records = self._from_text(message, text, seen_ids)

        return records

    def _from_html(
        self, message: dict[str, Any], html: str, seen_ids: set[str]
    ) -> list[JobRecord]:
        tree = HTMLParser(html)
        records: list[JobRecord] = []

        for anchor in tree.css("a"):
            href = anchor.attributes.get("href") or ""
            m = _JOB_URL_RE.search(href)
            if not m:
                continue
            source_id = m.group(1)
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            role = _txt(anchor).strip()
            if not role or len(role) < 3:
                # Try heading inside the anchor
                h = anchor.css_first("h3, h2, strong, span")
                role = _txt(h).strip()
            if not role or len(role) < 3:
                continue

            # Walk up 3 levels looking for company + location siblings
            company = ""
            location_raw = ""
            container = anchor.parent
            for _ in range(3):
                if container is None:
                    break
                blurb = _txt(container)
                lines = [line.strip() for line in blurb.splitlines() if line.strip()]
                # Company is usually the line right after the role title
                if len(lines) >= 2 and not company:
                    candidate = lines[1]
                    if len(candidate) > 1 and candidate.lower() != role.lower():
                        company = candidate
                if len(lines) >= 3 and not location_raw:
                    location_raw = lines[2]
                if company:
                    break
                container = container.parent

            location = _clean_location(location_raw)

            clean_href = href.split("?")[0]  # strip tracking params

            records.append(
                JobRecord(
                    source="gmail:linkedin",
                    source_id=source_id,
                    company=company or "Unknown",
                    role=role,
                    jd_text=f"{role} at {company or 'Unknown'}. {location or ''}".strip(),
                    jd_url=clean_href,
                    location=location,
                    remote=bool(location and "remote" in location.lower()),
                    raw_payload={"linkedin_job_id": source_id},
                )
            )

        return records

    @staticmethod
    def _from_text(
        message: dict[str, Any], text: str, seen_ids: set[str]
    ) -> list[JobRecord]:
        records: list[JobRecord] = []
        for m in _JOB_URL_RE.finditer(text):
            source_id = m.group(1)
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)
            records.append(
                JobRecord(
                    source="gmail:linkedin",
                    source_id=source_id,
                    company="Unknown",
                    role="Unknown",
                    jd_text="",
                    jd_url=f"https://www.linkedin.com/jobs/view/{source_id}",
                    raw_payload={"plaintext_fallback": True},
                )
            )
        return records
