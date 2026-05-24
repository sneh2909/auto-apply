"""Send an Application as an email via the Gmail API."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from autoapply.config import get_settings
from autoapply.db.models import Application, AppStatus
from autoapply.sources.gmail_alerts import GmailClient

log = logging.getLogger(__name__)


def _build_message(*, sender: str, sender_name: str, to: str, subject: str, body: str, attachment: Path | None) -> str:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment and attachment.exists():
        ctype, _ = mimetypes.guess_type(str(attachment))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with attachment.open("rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=attachment.name)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _send_sync(raw: str) -> dict:
    client = GmailClient()
    sent = client.service.users().messages().send(userId="me", body={"raw": raw}).execute()
    # Fetch the RFC822 Message-ID header so we can match replies back via In-Reply-To
    try:
        full = (
            client.service.users()
            .messages()
            .get(userId="me", id=sent["id"], format="metadata", metadataHeaders=["Message-ID"])
            .execute()
        )
        for h in full.get("payload", {}).get("headers", []):
            if h["name"].lower() == "message-id":
                sent["rfc822_message_id"] = h["value"]
                break
    except Exception:
        pass
    return sent


async def send_application(app: Application) -> Application:
    """Send the email built from `app.draft_*` fields and mark the application sent."""
    if app.status not in (AppStatus.approved, AppStatus.drafted):
        raise ValueError(f"application {app.id} not in approved/drafted state (status={app.status})")
    if not app.recipient or not app.draft_subject or not app.draft_body:
        raise ValueError(f"application {app.id} missing recipient/subject/body")

    settings = get_settings()
    resume = settings.resume_pdf if settings.resume_pdf.exists() else None

    raw = _build_message(
        sender=settings.gmail_user_email,
        sender_name=settings.gmail_sender_name,
        to=app.recipient,
        subject=app.draft_subject,
        body=app.draft_body,
        attachment=resume,
    )

    sent = await asyncio.to_thread(_send_sync, raw)
    log.info("sent application %s to %s (gmail id=%s)", app.id, app.recipient, sent.get("id"))

    app.status = AppStatus.sent
    app.sent_at = datetime.now(UTC)
    app.sender_account = settings.gmail_user_email
    app.sent_message_id = sent.get("rfc822_message_id")
    await app.save()
    return app
