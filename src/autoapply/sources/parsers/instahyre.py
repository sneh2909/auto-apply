"""Parser for Instahyre job-alert emails.

Instahyre alerts tend to be one job per email (or a small handful) with the
recruiter's name + email visible in the body — which is why this is one of the
highest-yield sources for `hr_email`. The HTML structure varies by template, so
we try several extraction paths:

  1. Header-anchor pattern: a heading link → role, sibling text → company.
  2. Plain-text fallback: scan the text body for "Role: X" / "Company: Y" / email.
  3. Generic anchor scan for `instahyre.com/c/jobs/<id>` URLs.

Drop a real fixture in tests/fixtures/instahyre_*.html when you have one and the
selectors below should adapt fast.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from autoapply.sources.base import EmailParser, JobRecord

log = logging.getLogger(__name__)

_JOB_URL_RE = re.compile(r"https?://(?:www\.)?instahyre\.com/(?:c/)?jobs?/(?:view/)?(\d+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_FIELD_RE = re.compile(
    r"(role|position|title|company|location|experience|exp|recruiter)\s*[:\-]\s*([^\n\r]+)",
    re.IGNORECASE,
)
_NON_PERSONAL_EMAIL = re.compile(r"(noreply|no-reply|donotreply|@instahyre\.com|hello@|support@)", re.IGNORECASE)


def _txt(node: Node | None) -> str:
    return node.text(strip=True, separator=" ").strip() if node else ""


def _parse_fields_from_text(text: str) -> dict[str, str]:
    """'Role: ML Engineer\\nCompany: Acme' → {'role': 'ML Engineer', 'company': 'Acme'}."""
    out: dict[str, str] = {}
    for label, value in _FIELD_RE.findall(text):
        key = label.lower()
        if key in ("position", "title"):
            key = "role"
        if key == "exp":
            key = "experience"
        out.setdefault(key, value.strip())
    return out


def _pick_personal_email(text: str) -> str | None:
    for e in _EMAIL_RE.findall(text):
        if not _NON_PERSONAL_EMAIL.search(e):
            return e
    return None


class InstahyreParser(EmailParser):
    name = "instahyre"
    sender_match = "instahyre.com"

    def parse(self, message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
        if not html and not text:
            return []
        tree = HTMLParser(html or "")

        # Pass 1 — anchors that point at instahyre job URLs.
        anchor_jobs = self._from_anchors(tree, message, text or tree.text(separator=" ", strip=True))
        if anchor_jobs:
            return anchor_jobs

        # Pass 2 — text-only fallback (the email body lays it out as labeled lines).
        plain = text or (tree.text(separator="\n", strip=True) if html else "")
        record = self._from_plaintext(message, plain)
        return [record] if record is not None else []

    def _from_anchors(self, tree: HTMLParser, message: dict[str, Any], body_text: str) -> list[JobRecord]:
        records: list[JobRecord] = []
        seen_ids: set[str] = set()

        for anchor in tree.css("a"):
            href = (anchor.attributes.get("href") or "")
            m = _JOB_URL_RE.search(href)
            if not m:
                continue
            source_id = m.group(1)
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            role = _txt(anchor)
            if not role or len(role) < 4:
                continue

            # Walk up to find sibling text that holds company / location.
            container = anchor.parent
            blurb = _txt(container)[:1500] if container else ""

            fields = _parse_fields_from_text(blurb + "\n" + (body_text or ""))
            company = fields.get("company") or self._guess_company_from_blurb(blurb, role) or "Unknown"
            location = fields.get("location")
            email = _pick_personal_email(blurb) or _pick_personal_email(body_text)
            recruiter = fields.get("recruiter")

            records.append(
                JobRecord(
                    source="gmail:instahyre",
                    source_id=source_id,
                    company=company.strip(),
                    role=role,
                    jd_text=blurb or body_text[:1500],
                    jd_url=href,
                    location=location,
                    remote=bool(location and "remote" in location.lower()),
                    recruiter_name=recruiter,
                    hr_email=email,
                    raw_payload={"href": href},
                )
            )
        return records

    @staticmethod
    def _from_plaintext(message: dict[str, Any], plain: str) -> JobRecord | None:
        if not plain:
            return None
        fields = _parse_fields_from_text(plain)
        role = fields.get("role")
        company = fields.get("company")
        if not (role and company):
            return None
        url_match = _JOB_URL_RE.search(plain)
        source_id = url_match.group(1) if url_match else message.get("id", "unknown")
        return JobRecord(
            source="gmail:instahyre",
            source_id=source_id,
            company=company,
            role=role,
            jd_text=plain[:1500],
            jd_url=url_match.group(0) if url_match else None,
            location=fields.get("location"),
            remote=bool(fields.get("location") and "remote" in fields["location"].lower()),
            recruiter_name=fields.get("recruiter"),
            hr_email=_pick_personal_email(plain),
            raw_payload={"plaintext_fallback": True},
        )

    @staticmethod
    def _guess_company_from_blurb(blurb: str, role: str) -> str | None:
        """If 'at <Company>' appears near the role, grab it."""
        if not blurb:
            return None
        m = re.search(r"\bat\s+([A-Z][\w&.\- ]{1,40}?)(?:\s*(?:\(|\||·|\.|,|$))", blurb)
        if m and role.lower() not in m.group(1).lower():
            return m.group(1).strip()
        return None
