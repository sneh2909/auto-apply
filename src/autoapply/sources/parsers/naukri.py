"""Parser for Naukri.com job-alert emails.

Naukri ships HTML emails with one job per visual card. Cards generally contain:
  - role title (anchor → naukri.com/job-listings-...)
  - company name (sibling element or below the title)
  - location and experience-required text

Selectors are written with multiple fallbacks; on parse failure we log the raw
HTML and let SourceRun.parse_errors increment. Add a fixture in tests when a
new layout appears.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser, Node

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)

_LISTING_HREF_RE = re.compile(r"(job-listings|/jd/|/job-detail)", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"-(\d{6,})(?:\?|$|/)")


def _txt(node: Node | None) -> str:
    return (node.text(strip=True, separator=" ") if node else "").strip()


def _nearest_company(title_node: Node) -> str:
    """Walk up to a card-ish container, then look for the next text element."""
    cur = title_node
    for _ in range(5):
        cur = cur.parent
        if cur is None:
            break
        cand = cur.css_first('[class*="company"], [class*="Company"], [id*="company"], font')
        if cand and _txt(cand) and _txt(cand).lower() != _txt(title_node).lower():
            return _txt(cand)
    return ""


def _extract_source_id(href: str) -> str:
    m = _JOB_ID_RE.search(href)
    return m.group(1) if m else urlparse(href).path.rstrip("/").split("/")[-1] or href


class NaukriParser(EmailParser):
    name = "naukri"
    sender_match = "naukri.com"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        if not html:
            return []
        tree = HTMLParser(html)
        records: list[JobRecord] = []
        seen_ids: set[str] = set()

        for anchor in tree.css("a"):
            href = anchor.attributes.get("href") or ""
            if not _LISTING_HREF_RE.search(href):
                continue
            title = _txt(anchor)
            if not title or len(title) < 4:
                continue
            source_id = _extract_source_id(href)
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            company = _nearest_company(anchor)
            # Try to grab a small contextual blurb around the card for the JD field.
            container = anchor.parent or anchor
            blurb = _txt(container)[:1200]

            # Location guess — look for "Location:" or city tokens in the blurb.
            loc = ""
            loc_match = re.search(
                r"(Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Delhi|Chennai|Gurgaon|Noida|Remote)",
                blurb,
                re.IGNORECASE,
            )
            if loc_match:
                loc = loc_match.group(1)

            records.append(
                JobRecord(
                    source="gmail:naukri",
                    source_id=source_id,
                    company=company or "Unknown",
                    role=title,
                    jd_text=blurb,
                    jd_url=href,
                    location=loc or None,
                    remote=("remote" in loc.lower()) if loc else False,
                    raw_payload={"href": href},
                )
            )

        if not records:
            log.info("Naukri parser found 0 jobs in message %s", message.get("id"))
        return records
