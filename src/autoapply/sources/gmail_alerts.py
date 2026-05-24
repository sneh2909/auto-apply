"""Gmail-driven ingestion.

Reads messages from `JobAlerts/Unprocessed`, dispatches each to the parser whose
`sender_match` is found in the From: header, then moves the message to
`JobAlerts/Processed`. Idempotent thanks to the label transition and the
dedup_hash unique index on Job.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from autoapply.config import get_settings
from autoapply.db.models import SourceRun
from autoapply.sources.base import EmailParser, JobRecord, Source, upsert_jobs

log = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


# === Low-level Gmail client (sync; wrap with asyncio.to_thread when needed) ===


class GmailClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._service: Resource | None = None
        self._label_cache: dict[str, str] = {}

    @cached_property
    def credentials(self) -> Credentials:
        path = self.settings.gmail_token_path
        if not path.exists():
            raise RuntimeError(
                f"No Gmail token at {path}. Run `python scripts/setup_gmail_oauth.py` first."
            )
        creds = Credentials.from_authorized_user_file(str(path), GMAIL_SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                path.write_text(creds.to_json())
            else:
                raise RuntimeError("Gmail credentials invalid; re-run setup_gmail_oauth.py.")
        return creds

    @property
    def service(self) -> Resource:
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)
        return self._service

    def label_id(self, name: str) -> str:
        if name in self._label_cache:
            return self._label_cache[name]
        resp = self.service.users().labels().list(userId="me").execute()
        for label in resp.get("labels", []):
            if label["name"] == name:
                self._label_cache[name] = label["id"]
                return label["id"]
        # Create if missing
        created = (
            self.service.users()
            .labels()
            .create(userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
            .execute()
        )
        self._label_cache[name] = created["id"]
        return created["id"]

    def list_messages(self, query: str = "", label_ids: list[str] | None = None, max_results: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"userId": "me", "maxResults": max_results}
        if query:
            params["q"] = query
        if label_ids:
            params["labelIds"] = label_ids
        resp = self.service.users().messages().list(**params).execute()
        return resp.get("messages", [])

    def get_message(self, message_id: str) -> dict[str, Any]:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def modify_labels(self, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        if body:
            self.service.users().messages().modify(userId="me", id=message_id, body=body).execute()


# === Body extraction ======================================================


def _decode_part(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def extract_bodies(message: dict[str, Any]) -> tuple[str, str]:
    """Return (html, text). Either may be empty."""
    html_parts: list[str] = []
    text_parts: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        mime = node.get("mimeType", "")
        if mime == "text/html":
            html_parts.append(_decode_part(node))
        elif mime == "text/plain":
            text_parts.append(_decode_part(node))
        for child in node.get("parts", []):
            walk(child)

    payload = message.get("payload", {})
    walk(payload)
    return "\n".join(html_parts), "\n".join(text_parts)


def header(message: dict[str, Any], name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# === The source ===========================================================


class GmailAlertSource(Source):
    name = "gmail_alerts"

    def __init__(self, parsers: list[EmailParser]) -> None:
        self.parsers = parsers
        self.client = GmailClient()

    async def fetch(self) -> list[JobRecord]:
        settings = get_settings()
        run = SourceRun(source=self.name, started_at=datetime.now(UTC))
        await run.insert()

        all_records: list[JobRecord] = []

        try:
            unprocessed_label = await asyncio.to_thread(self.client.label_id, settings.gmail_label_unprocessed)
            processed_label = await asyncio.to_thread(self.client.label_id, settings.gmail_label_processed)

            messages = await asyncio.to_thread(
                self.client.list_messages, label_ids=[unprocessed_label], max_results=50
            )
            run.emails_seen = len(messages)

            for m in messages:
                msg_id = m["id"]
                try:
                    full = await asyncio.to_thread(self.client.get_message, msg_id)
                    from_hdr = header(full, "From")
                    subject = header(full, "Subject")
                    html, text = extract_bodies(full)

                    parser = next(
                        (
                            p
                            for p in self.parsers
                            if p.matches_message(full, from_hdr, subject, html, text)
                        ),
                        None,
                    )
                    if parser is not None:
                        records = parser.parse(full, html, text)
                        if not records and parser.name in ("foundit", "hirect", "wellfound"):
                            parser = None
                    if parser is None:
                        from autoapply.sources.parsers.generic_llm import extract_jobs_via_llm

                        log.info(
                            "no parser / empty parse for From=%s — LLM fallback (msg %s)",
                            from_hdr,
                            msg_id,
                        )
                        records = await extract_jobs_via_llm(full, html, text)

                    for r in records:
                        r.raw_payload.setdefault("gmail_message_id", msg_id)
                    all_records.extend(records)
                    run.jobs_emitted += len(records)
                except Exception as e:
                    run.parse_errors += 1
                    log.exception("parse error for message %s: %s", msg_id, e)
                else:
                    await asyncio.to_thread(
                        self.client.modify_labels,
                        msg_id,
                        add=[processed_label],
                        remove=[unprocessed_label],
                    )

            inserted, skipped = await upsert_jobs(all_records)
            run.notes = f"inserted={inserted} skipped={skipped}"
        finally:
            run.finished_at = datetime.now(UTC)
            await run.save()

        return all_records
