"""Parser for IBM Kenexa / Jobs2Web talent-community job-alert emails.

Typical senders:
  deloittesh-jobnotification@noreply44.jobs2web.com
  mahindra-jobnotification@noreply.jobs2web.com

Typical body (plain text):
  New jobs posted from southasiacareers.deloitte.com
  ...
  Your Job Alert matched the following jobs at jobs.mahindracareers.com.

  Jobs
  AI Engineer - Mumbai - Worli, Mumbai - Worli, IN
  T&T| Azure Data Engineer | Manager | Bengaluru - Bengaluru, IN
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from autoapply.sources.base import EmailParser, JobRecord
from autoapply.sources.gmail_alerts import header

log = logging.getLogger(__name__)

_CAREER_SITE_RE = re.compile(
    r"(?:new jobs posted from|matched the following jobs at)\s+"
    r"([a-z0-9][a-z0-9.-]*\.[a-z]{2,})",
    re.IGNORECASE,
)
_IN_SUFFIX_RE = re.compile(r",\s*IN\s*$", re.IGNORECASE)
_HREF_RE = re.compile(
    r'href=["\'](https?://[^"\']+)["\']',
    re.IGNORECASE,
)
_SKIP_LINE_PREFIXES = (
    "you are receiving",
    "your job alert",
    "your job agent",
    "unsubscribe",
    "update your preferences",
    "add another job",
    "change the job",
    "remember to forward",
    "a password has been",
    "jobs",  # section header only
)


def _company_from_domain(domain: str) -> str:
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 2:
        # southasiacareers.deloitte.com → deloitte; jobs.mahindracareers.com → mahindra
        brand = parts[-2]
        if brand in {"com", "co", "jobs", "careers", "www"} and len(parts) >= 3:
            brand = parts[-3]
        for suffix in ("careers", "jobs", "talent", "recruiting"):
            if brand.endswith(suffix) and len(brand) > len(suffix):
                brand = brand[: -len(suffix)]
        if brand.startswith("jobs"):
            brand = brand[4:]
        return brand.replace("-", " ").title()
    return domain


def _source_id(company: str, role: str, location: str) -> str:
    key = f"{company}|{role}|{location}".lower()
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _split_job_line(line: str) -> tuple[str, str] | None:
    """Parse '{title} - {location}, IN' lines from talent-community digests."""
    line = line.strip()
    if not line or not _IN_SUFFIX_RE.search(line):
        return None
    body = _IN_SUFFIX_RE.sub("", line).strip()
    if " - " not in body:
        return None
    role, location = body.rsplit(" - ", 1)
    role, location = role.strip(), location.strip()
    if len(role) < 4 or len(location) < 2:
        return None
    return role, location


def _parse_job_lines(text: str, career_site: str, company: str) -> list[JobRecord]:
    records: list[JobRecord] = []
    seen: set[str] = set()
    base_url = f"https://{career_site}/" if career_site else None

    for line in text.splitlines():
        parsed = _split_job_line(line)
        if not parsed:
            continue
        role, location = parsed
        low = role.lower()
        if any(low.startswith(p) for p in _SKIP_LINE_PREFIXES):
            continue
        sid = _source_id(company, role, location)
        if sid in seen:
            continue
        seen.add(sid)
        remote = "remote" in location.lower() or "remote" in role.lower()
        records.append(
            JobRecord(
                source="gmail:jobs2web",
                source_id=sid,
                company=company,
                role=role,
                jd_text=f"{role} — {location} ({career_site})",
                jd_url=base_url,
                location=location,
                remote=remote,
                raw_payload={"career_site": career_site, "parser": "jobs2web_text"},
            )
        )
    return records


def _parse_html_links(html: str, career_site: str, company: str) -> list[JobRecord]:
    if not html:
        return []
    records: list[JobRecord] = []
    seen: set[str] = set()
    tree = HTMLParser(html)
    for anchor in tree.css("a"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc.lower()
        if career_site and career_site not in host:
            continue
        title = (anchor.text(strip=True) or "").strip()
        if len(title) < 4 or len(title) > 200:
            continue
        sid = _source_id(company, title, href)
        if sid in seen:
            continue
        seen.add(sid)
        records.append(
            JobRecord(
                source="gmail:jobs2web",
                source_id=sid,
                company=company,
                role=title,
                jd_text=title,
                jd_url=href,
                location=None,
                raw_payload={"career_site": career_site, "parser": "jobs2web_html"},
            )
        )
    return records


class Jobs2WebParser(EmailParser):
    """Talent-community digests (Deloitte, Mahindra, and other Jobs2Web-hosted sites)."""

    name = "jobs2web"
    sender_match = "jobs2web.com"

    def matches_message(
        self,
        message: dict[str, Any],
        from_header: str,
        subject: str,
        html: str,
        text: str,
    ) -> bool:
        if self.matches(from_header):
            return True
        blob = f"{subject}\n{text}\n{html}".lower()
        return (
            "new jobs posted from" in blob
            or "matched the following jobs at" in blob
            or "talent community" in blob and "matched the following jobs" in blob
        )

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        body = text or ""
        if html and len(body) < 80:
            try:
                body = HTMLParser(html).text(separator="\n", strip=True)
            except Exception:
                pass

        subject = header(message, "Subject")
        combined = f"{subject}\n{body}"
        career_site = ""
        site_m = _CAREER_SITE_RE.search(combined)
        if site_m:
            career_site = site_m.group(1).lower().strip(".")
        else:
            for m in re.finditer(
                r"([a-z0-9][a-z0-9.-]*careers[a-z0-9.-]*\.[a-z]{2,})",
                combined,
                re.I,
            ):
                career_site = m.group(1).lower().strip(".")
                break
        if not career_site:
            log.info("jobs2web parser: no career site in message %s", message.get("id"))
            return []
        company = _company_from_domain(career_site)

        records = _parse_job_lines(body, career_site, company)
        if html:
            for rec in _parse_html_links(html, career_site, company):
                if rec.source_id not in {r.source_id for r in records}:
                    records.append(rec)

        if not records:
            log.info(
                "jobs2web parser: 0 jobs from %s (site=%s)",
                message.get("id"),
                career_site,
            )
        return records
