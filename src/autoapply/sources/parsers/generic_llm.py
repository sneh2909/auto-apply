"""LLM-based fallback parser for unknown-sender job emails.

When no specific parser (Naukri/LinkedIn/Foundit/etc.) matches a Gmail message,
this falls back to a single LLM call that decides:
  - Is this even a job email?
  - If yes: extract company, role, JD text, JD URL, and recruiter email.

Used from `gmail_alerts.py` as an awaited helper - unlike the regex parsers,
it's async because it talks to an LLM.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from selectolax.parser import HTMLParser

from autoapply.score.llm import get_agent_llm
from autoapply.sources.base import JobRecord
from autoapply.sources.gmail_alerts import header

log = logging.getLogger(__name__)


_PROMPT = """\
You receive the headers and body of an email. Decide whether it is a job
announcement (recruiter outreach, alert digest, job post, hiring referral).

If YES, extract job info. If NO (newsletter, marketing, OTP, transactional, etc.),
return is_job=false.

Return strict JSON, this exact shape:
{
  "is_job": true,
  "jobs": [
    {
      "company": "Acme AI",
      "role":    "Senior ML Engineer",
      "location": "Bangalore" or null,
      "jd_url":   "https://..." or null,
      "hr_email": "recruiter@acme.com" or null,
      "summary":  "1-2 sentence summary of the role"
    }
  ]
}

If is_job=false return {"is_job": false, "jobs": []}.
Multiple jobs in one email = multiple entries in jobs[].

EMAIL HEADERS:
From:    %(from)s
Subject: %(subject)s
Reply-To: %(reply_to)s

EMAIL BODY (text):
%(text)s
"""


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PERSONAL_BLOCKED = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "notifications",
}


def _text_from_html(html: str) -> str:
    if not html:
        return ""
    try:
        return HTMLParser(html).text(separator=" ", strip=True)
    except Exception:
        return ""


def _looks_personal(email: str | None) -> bool:
    if not email:
        return False
    local = email.split("@", 1)[0].lower()
    return not any(b in local for b in _PERSONAL_BLOCKED)


def _extract_from_email_header(value: str) -> tuple[str, str]:
    """Parse 'Name <email@host>' → (name, email)."""
    if not value:
        return "", ""
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<([^>]+)>\s*$', value)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if _EMAIL_RE.fullmatch(value.strip()):
        return "", value.strip()
    return value.strip(), ""


async def extract_jobs_via_llm(message: dict[str, Any], html: str, text: str) -> list[JobRecord]:
    """LLM-driven extraction; returns [] if this doesn't look like a job email."""
    from_hdr = header(message, "From")
    subject = header(message, "Subject")
    reply_to = header(message, "Reply-To")
    msg_id = message.get("id", "")

    # Prefer plain text; fall back to text-stripped HTML
    body_text = (text or _text_from_html(html))[:4500]
    if not body_text.strip():
        return []

    sender_name, sender_email = _extract_from_email_header(from_hdr)
    _, reply_email = _extract_from_email_header(reply_to)
    fallback_recipient = reply_email or (sender_email if _looks_personal(sender_email) else "")

    prompt = _PROMPT % {
        "from": from_hdr or "",
        "subject": subject or "",
        "reply_to": reply_to or "",
        "text": body_text,
    }

    try:
        llm = get_agent_llm()
        out = await llm.chat_json(
            [
                {"role": "system", "content": "Return strict JSON only. No prose, no markdown fences."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=800,
        )
    except Exception:
        log.exception("generic_llm extract failed for msg %s", msg_id)
        return []

    if not out or not out.get("is_job"):
        return []

    records: list[JobRecord] = []
    for raw in (out.get("jobs") or []):
        company = (raw.get("company") or "").strip()
        role = (raw.get("role") or "").strip()
        if not company or not role:
            continue

        # Pick the best HR email: LLM's hr_email > Reply-To > a personal-looking From
        hr_email = (raw.get("hr_email") or "").strip()
        if not _looks_personal(hr_email):
            hr_email = ""
        if not hr_email and fallback_recipient:
            hr_email = fallback_recipient

        # Body emails as a last-resort scan
        if not hr_email:
            for found in _EMAIL_RE.findall(body_text)[:5]:
                if _looks_personal(found) and "@" in found:
                    hr_email = found
                    break

        record = JobRecord(
            source="gmail:generic_llm",
            source_id=msg_id,
            company=company,
            role=role,
            jd_text=raw.get("summary") or body_text[:2000],
            jd_url=(raw.get("jd_url") or "").strip() or None,
            location=(raw.get("location") or None),
            recruiter_name=sender_name or None,
            hr_email=hr_email or None,
            raw_payload={"gmail_message_id": msg_id, "extractor": "generic_llm"},
        )
        records.append(record)

    if records:
        log.info("generic_llm extracted %d job(s) from msg %s (sender=%s)", len(records), msg_id, from_hdr)
    return records
