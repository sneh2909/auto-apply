"""Detect replies to sent applications and classify them.

Strategy:
  1. List inbox messages newer_than:14d.
  2. For each, read In-Reply-To and References headers.
  3. Match either header against `Application.sent_message_id` of any sent app.
  4. If matched and we don't already have a Reply row for this gmail_message_id,
     classify the snippet via the agent LLM, then insert a Reply doc.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from autoapply.db import get_db
from autoapply.db.models import Application, Reply, ReplyClass
from autoapply.score.llm import get_agent_llm
from autoapply.sources.gmail_alerts import GmailClient, header

log = logging.getLogger(__name__)


_CLASSIFY_PROMPT = (
    "Classify this email reply to a job application into ONE of these labels:\n"
    "  - interest:  recruiter is interested, wants to talk, schedule a call, share JD, etc.\n"
    "  - reject:    rejection (politely declined, position filled, not a fit).\n"
    "  - auto_ack:  automated acknowledgement (we received your application, etc.).\n"
    "  - other:     anything else (out of office, requests for more info, spam).\n"
    "Return strict JSON: {\"classification\": \"interest|reject|auto_ack|other\"}.\n"
    "Snippet:\n"
)


async def _classify(snippet: str) -> ReplyClass:
    if not snippet.strip():
        return ReplyClass.other
    try:
        llm = get_agent_llm()
        out = await llm.chat_json(
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": _CLASSIFY_PROMPT + snippet[:1500]},
            ],
            temperature=0.0,
            max_tokens=64,
        )
        label = (out.get("classification") or "").lower().strip()
        try:
            return ReplyClass(label)
        except ValueError:
            return ReplyClass.other
    except Exception:
        log.exception("classify_reply LLM call failed")
        return ReplyClass.other


def _normalize_message_id(value: str) -> str:
    """Strip angle brackets and whitespace from a Message-ID header value."""
    return (value or "").strip().strip("<>").strip()


async def check_replies() -> int:
    """Look for new replies to sent applications. Returns count of new Reply rows."""
    log.info("--- reply watcher started ---")

    db = get_db()
    app_coll = db[Application.collection]
    reply_coll = db[Reply.collection]

    # Index all applications that have a sent_message_id we can match against
    apps_by_msgid: dict[str, str] = {}
    async for raw in app_coll.find(
        {"sent_message_id": {"$ne": None}},
        {"_id": 1, "sent_message_id": 1},
    ):
        mid = _normalize_message_id(raw.get("sent_message_id", ""))
        if mid:
            apps_by_msgid[mid] = str(raw["_id"])

    if not apps_by_msgid:
        log.info("no sent applications with message-ids — nothing to track")
        return 0

    # Pull inbox messages
    try:
        client = GmailClient()
    except RuntimeError as e:
        log.warning("reply watcher skipped: %s", e)
        return 0

    messages = await asyncio.to_thread(
        client.list_messages, query="in:inbox newer_than:14d", max_results=100
    )
    if not messages:
        return 0

    new_replies = 0
    for m in messages:
        msg_id = m["id"]
        # Already recorded?
        existing = await reply_coll.find_one({"gmail_message_id": msg_id})
        if existing:
            continue

        try:
            full = await asyncio.to_thread(client.get_message, msg_id)
        except Exception:
            log.exception("get_message failed for %s", msg_id)
            continue

        in_reply_to = _normalize_message_id(header(full, "In-Reply-To"))
        references_raw = header(full, "References") or ""
        ref_ids = [_normalize_message_id(r) for r in references_raw.split() if r.strip()]

        matched_app_id: str | None = None
        if in_reply_to and in_reply_to in apps_by_msgid:
            matched_app_id = apps_by_msgid[in_reply_to]
        else:
            for rid in ref_ids:
                if rid in apps_by_msgid:
                    matched_app_id = apps_by_msgid[rid]
                    break

        if not matched_app_id:
            continue

        snippet = full.get("snippet", "")
        classification = await _classify(snippet)

        reply = Reply(
            application_id=matched_app_id,
            gmail_message_id=msg_id,
            snippet=snippet[:500],
            classification=classification,
            received_at=datetime.now(UTC),
        )
        await reply.insert()
        new_replies += 1
        log.info(
            "reply for app %s classified as %s (msg %s): %s…",
            matched_app_id, classification.value, msg_id, snippet[:80],
        )

    log.info("--- reply watcher done: %d new replies ---", new_replies)
    return new_replies
