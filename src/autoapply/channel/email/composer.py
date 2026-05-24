"""Compose the application email body via LLM.

Output schema is strict JSON: {subject, body, confidence}.
We pin the model to "use facts only from the supplied profile" to suppress hallucination.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from autoapply.config import Profile, get_profile
from autoapply.db.models import Job
from autoapply.score.llm import get_composer_llm

log = logging.getLogger(__name__)

_JD_MAX_CHARS = 4000


@dataclass(slots=True)
class ComposedEmail:
    subject: str
    body: str
    confidence: float


def _build_prompt(profile: Profile, job: Job, recruiter_name: str | None) -> str:
    profile_blob = json.dumps(profile.for_prompt(), indent=2, ensure_ascii=False)
    salutation_hint = f"Address by name: {recruiter_name}" if recruiter_name else "No name available — use 'Hi there,'"
    return f"""\
Compose a cold-application email for the role below.

CANDIDATE PROFILE (use ONLY these facts — do not invent companies, metrics, or skills):
{profile_blob}

JOB:
Company: {job.company}
Role:    {job.role}
URL:     {job.jd_url or "n/a"}
Loc:     {job.location or "unspecified"}

JD (truncated):
{(job.jd_text or "")[:_JD_MAX_CHARS]}

Recipient: {salutation_hint}

Requirements:
  - 4 to 6 sentences total. No paragraphs longer than 2 sentences.
  - Reference TWO specific things from the JD (technology, problem, scale).
  - Reference ONE highlight bullet from the profile that matches.
  - End with a clear ask: short call this week, calendar link omitted (will be added by sender).
  - Subject: "[Application] {{role}} — {{candidate first name}}" style; under 70 chars.
  - Tone: confident, terse, zero filler. No "I hope this email finds you well".
  - Include this exact opt-out line as the final line: "If you'd rather I didn't follow up, reply STOP."

Return strict JSON only, exactly:
{{
  "subject": "...",
  "body": "...",
  "confidence": 0.0
}}
"""


async def compose_email(job: Job, recruiter_name: str | None = None, profile: Profile | None = None) -> ComposedEmail:
    profile = profile or get_profile()
    llm = get_composer_llm()
    raw = await llm.chat_json(
        [
            {"role": "system", "content": "You return strict JSON only. Do not invent facts about the candidate."},
            {"role": "user", "content": _build_prompt(profile, job, recruiter_name)},
        ],
        temperature=0.4,
        max_tokens=600,
    )
    return ComposedEmail(
        subject=str(raw.get("subject", "")).strip()[:120],
        body=str(raw.get("body", "")).strip(),
        confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
    )
