"""FastAPI review UI.

Routes:
  GET  /                queue page (pending applications)
  POST /draft/{id}      generate email draft via LLM (step 1)
  POST /approve/{id}    send the drafted email (step 2)
  POST /skip/{id}       mark skipped
  POST /agent           kick off a Channel D run
  GET  /events          SSE stream for live stats + toast notifications
  POST /run/ingest      run ingestion pipeline
  POST /run/score       run scoring pipeline
  POST /run/route       run routing pipeline
  POST /run/full        run full pipeline
  GET  /unapplied       failed applications + unrouted scored jobs
  POST /retry/{id}      reset failed application back to queued
  GET  /blocklist       show blocklist
  POST /blocklist/add   add entry
  POST /blocklist/delete remove entry
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from bson import ObjectId
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from autoapply.channel.agent.run import run_agent
from autoapply.channel.email.composer import compose_email
from autoapply.channel.email.sender import send_application
from autoapply.config import Blocklist, get_settings
from autoapply.db import close_db, get_db, init_db
from autoapply.db.models import Application, AppStatus, Channel, Job
from autoapply.scheduler import ingest_once, route_once, score_once

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_EMAIL_CHANNELS = {Channel.email_reply.value, Channel.coldmail.value}

# === SSE broadcaster =========================================================
# Each connected /events client gets its own asyncio.Queue.

_sse_clients: set[asyncio.Queue] = set()


def broadcast(event: dict) -> None:
    """Push a JSON event to every connected SSE client (fire-and-forget)."""
    payload = json.dumps(event)
    dead: set[asyncio.Queue] = set()
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_clients.difference_update(dead)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(title="auto-apply review queue", lifespan=lifespan)


# === Blocklist file I/O (bypass lru_cache) ===================================


def _blocklist_path() -> Path:
    return get_settings().config_dir / "blocklist.yaml"


def _read_blocklist() -> Blocklist:
    path = _blocklist_path()
    if not path.exists():
        return Blocklist()
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Blocklist.model_validate(raw)


def _write_blocklist(bl: Blocklist) -> None:
    data: dict = {"companies": bl.companies, "domains": bl.domains}
    if bl.notes:
        data["notes"] = bl.notes
    _blocklist_path().write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


# === Helpers =================================================================


async def _sent_today_count() -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    coll = get_db()[Application.collection]
    return await coll.count_documents(
        {"status": {"$in": [AppStatus.sent.value, AppStatus.submitted.value]}, "sent_at": {"$gte": start}}
    )


async def _list_pending() -> list[dict]:
    coll = get_db()[Application.collection]
    cursor = coll.find({"status": {"$in": [AppStatus.queued.value, AppStatus.drafted.value]}})
    items: list[dict] = []
    async for a in cursor:
        job = await get_db()[Job.collection].find_one({"_id": ObjectId(a["job_id"])})
        if job is None:
            continue
        channel = a.get("channel", "")
        subject = a.get("draft_subject") or ""
        body = a.get("draft_body") or ""
        items.append(
            {
                "id": str(a["_id"]),
                "company": job.get("company", ""),
                "role": job.get("role", ""),
                "channel": channel,
                "score": job.get("fit_score") or 0.0,
                "recipient": a.get("recipient") or a.get("ats_url") or "",
                "job_url": job.get("jd_url") or "",
                "is_email": channel in _EMAIL_CHANNELS,
                "has_draft": bool(subject),
                "subject": subject,
                "body": body,
                "body_preview": body[:300],
            }
        )
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


async def _list_for_apply() -> dict[str, list[dict]]:
    """Return apps grouped by 'ready' (approved), 'done' (sent/submitted), 'failed'."""
    db = get_db()
    app_coll = db[Application.collection]
    job_coll = db[Job.collection]

    buckets: dict[str, list[dict]] = {"ready": [], "done": [], "failed": []}
    bucket_for = {
        AppStatus.approved.value: "ready",
        AppStatus.sent.value: "done",
        AppStatus.submitted.value: "done",
        AppStatus.failed.value: "failed",
    }

    async for a in app_coll.find({"status": {"$in": list(bucket_for)}}):
        bucket = bucket_for[a["status"]]
        job = await job_coll.find_one({"_id": ObjectId(a["job_id"])})
        if job is None:
            continue
        channel = a.get("channel", "")
        jd_url = job.get("jd_url") or ""
        ats_kind = ""
        if channel == Channel.ats.value and jd_url:
            from autoapply.channel.router import _detect_ats
            ats_kind = (_detect_ats(jd_url) or "").split(":", 1)[-1]

        buckets[bucket].append(
            {
                "id": str(a["_id"]),
                "company": job.get("company", ""),
                "role": job.get("role", ""),
                "channel": channel,
                "ats_kind": ats_kind,
                "score": job.get("fit_score") or 0.0,
                "recipient": a.get("recipient") or "",
                "job_url": jd_url,
                "ats_url": a.get("ats_url") or jd_url,
                "is_email": channel in _EMAIL_CHANNELS,
                "subject": a.get("draft_subject") or "",
                "body_preview": (a.get("draft_body") or "")[:200],
                "status": a["status"],
                "error": a.get("error") or "",
                "sent_at": a.get("sent_at"),
            }
        )

    for items in buckets.values():
        items.sort(key=lambda x: x["score"], reverse=True)
    return buckets


async def _dashboard_stats() -> dict[str, int]:
    db = get_db()
    return {
        "jobs": await db[Job.collection].count_documents({}),
        "unscored": await db[Job.collection].count_documents({"fit_score": None}),
        "queued": await db[Application.collection].count_documents(
            {"status": {"$in": [AppStatus.queued.value, AppStatus.drafted.value]}}
        ),
        "approved": await db[Application.collection].count_documents(
            {"status": AppStatus.approved.value}
        ),
        "submitted": await db[Application.collection].count_documents(
            {"status": {"$in": [AppStatus.sent.value, AppStatus.submitted.value]}}
        ),
        "failed": await db[Application.collection].count_documents(
            {"status": AppStatus.failed.value}
        ),
    }


# === Routes ==================================================================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "items": await _list_pending(),
            "sent_today": await _sent_today_count(),
            "daily_cap": settings.daily_send_cap,
            "stats": await _dashboard_stats(),
            "message": request.query_params.get("message", ""),
        },
    )


@app.get("/apply", response_class=HTMLResponse)
async def apply_page(request: Request):
    settings = get_settings()
    buckets = await _list_for_apply()
    return templates.TemplateResponse(
        request,
        "apply.html",
        {
            "ready": buckets["ready"],
            "done": buckets["done"],
            "failed": buckets["failed"],
            "sent_today": await _sent_today_count(),
            "daily_cap": settings.daily_send_cap,
            "stats": await _dashboard_stats(),
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/run/ingest")
async def run_ingest():
    broadcast({"type": "status", "message": "Ingest started…"})
    await ingest_once()
    stats = await _dashboard_stats()
    broadcast({"type": "done", "message": "Ingest complete.", "stats": stats})
    return RedirectResponse(url=f"/?message={quote('Ingest complete')}", status_code=303)


@app.post("/run/score")
async def run_score(limit: int = Form(100)):
    broadcast({"type": "status", "message": "Scoring started…"})
    scored = await score_once(limit=limit)
    stats = await _dashboard_stats()
    broadcast({"type": "done", "message": f"Scored {scored} jobs.", "stats": stats})
    return RedirectResponse(url=f"/?message={quote(f'Scored {scored} jobs')}", status_code=303)


@app.post("/run/route")
async def run_route(limit: int = Form(50)):
    broadcast({"type": "status", "message": "Routing started…"})
    routed = await route_once(limit=limit)
    stats = await _dashboard_stats()
    broadcast({"type": "done", "message": f"Routed {routed} applications.", "stats": stats})
    return RedirectResponse(url=f"/?message={quote(f'Routed {routed} applications')}", status_code=303)


@app.post("/run/full")
async def run_full():
    broadcast({"type": "status", "message": "Full pipeline started…"})
    await ingest_once()
    broadcast({"type": "status", "message": "Ingest done, scoring…"})
    scored = await score_once(limit=100)
    broadcast({"type": "status", "message": f"Scored {scored}, routing…"})
    routed = await route_once(limit=50)
    message = f"Pipeline complete: scored {scored}, routed {routed}"
    stats = await _dashboard_stats()
    broadcast({"type": "done", "message": message, "stats": stats})
    return RedirectResponse(
        url=f"/?message={quote(message)}",
        status_code=303,
    )


@app.post("/draft/{app_id}")
async def generate_draft(app_id: str):
    """Step 1: compose the email via LLM and save it as a draft."""
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")

    job_raw = await get_db()[Job.collection].find_one({"_id": ObjectId(a.job_id)})
    if job_raw is None:
        raise HTTPException(status_code=404, detail="job not found")
    job = Job.model_validate(job_raw)

    try:
        composed = await compose_email(job, recruiter_name=job.recruiter_name)
    except Exception as e:
        log.exception("compose failed for application %s: %s", app_id, e)
        raise HTTPException(status_code=500, detail=f"compose failed: {e}") from e

    a.draft_subject = composed.subject
    a.draft_body = composed.body
    a.status = AppStatus.drafted
    await a.save()
    log.info("drafted application %s (confidence=%.2f)", app_id, composed.confidence)

    return RedirectResponse(url="/", status_code=303)


@app.post("/approve/{app_id}")
async def approve(
    app_id: str,
    draft_subject: str = Form(None),
    draft_body: str = Form(None),
):
    """Pipeline step: mark an application as approved. Apply happens later from /apply."""
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")

    # Accept inline edits from the form
    if draft_subject:
        a.draft_subject = draft_subject.strip()
    if draft_body:
        a.draft_body = draft_body.strip()

    a.status = AppStatus.approved
    await a.save()
    broadcast({"type": "status", "message": f"Approved: {a.draft_subject or a.recipient or a.ats_url or app_id}"})

    return RedirectResponse(url="/", status_code=303)


@app.post("/unapprove/{app_id}")
async def unapprove(app_id: str):
    """Move an approved application back to the review queue."""
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")
    a.status = AppStatus.drafted if a.draft_subject else AppStatus.queued
    await a.save()
    return RedirectResponse(url="/apply", status_code=303)


# Track in-flight background tasks so they're not garbage-collected mid-run
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def _execute_apply(app_id: str, *, label_prefix: str = "") -> tuple[bool, str]:
    """Apply a single application. Updates DB + broadcasts. Returns (ok, message).

    Shared by /apply/{id} (single click) and /apply/bulk (multi-select).
    """
    settings = get_settings()
    a = await Application.get(app_id)
    if a is None:
        return False, "application not found"
    if a.status != AppStatus.approved:
        return False, f"skipped (status={a.status})"

    label_for_log = (a.draft_subject or a.recipient or a.ats_url or app_id)[:60]
    prefix = f"{label_prefix} " if label_prefix else ""

    # ── Email channels ────────────────────────────────────────────
    if a.channel in (Channel.email_reply.value, Channel.coldmail.value):
        if await _sent_today_count() >= settings.daily_send_cap:
            return False, "daily send cap reached"
        broadcast({"type": "status", "message": f"{prefix}Sending → {a.recipient}…"})
        try:
            await send_application(a)
            return True, f"sent → {a.recipient}"
        except Exception as e:
            log.exception("send failed for %s", label_for_log)
            a.status = AppStatus.failed
            a.error = str(e)
            await a.save()
            return False, f"send failed: {e}"

    # ── ATS channels ──────────────────────────────────────────────
    if a.channel == Channel.ats.value:
        from autoapply.channel.autofill.base import get_filler
        from autoapply.channel.autofill.portal_apply import PortalApplyFailed, PortalLoginRequired
        from autoapply.channel.router import _detect_ats

        job_raw = await get_db()[Job.collection].find_one(
            {"_id": __import__("bson").ObjectId(a.job_id)}
        )
        ats_label = ""
        if job_raw and job_raw.get("jd_url"):
            ats_label = _detect_ats(job_raw["jd_url"]) or ""

        filler = get_filler(ats_label)
        if filler is None:
            msg = f"no autofiller for {ats_label or 'unknown ATS'}"
            a.status = AppStatus.failed
            a.error = msg
            await a.save()
            return False, msg

        broadcast({"type": "status", "message": f"{prefix}Auto-applying via {filler.name}: {label_for_log}…"})
        try:
            await filler.apply(app_id)
            return True, f"submitted via {filler.name}"
        except PortalLoginRequired as e:
            log.warning("apply blocked (login): %s", e)
            return False, f"{filler.name}: login required"
        except PortalApplyFailed as e:
            log.warning("apply failed: %s", e)
            return False, f"{filler.name}: {e}"
        except Exception as e:
            log.exception("apply unexpected error")
            a.status = AppStatus.failed
            a.error = f"unexpected: {e}"
            await a.save()
            return False, f"unexpected: {e}"

    return False, f"unsupported channel: {a.channel}"


# ── Bulk apply ────────────────────────────────────────────────────────────
# NOTE: /apply/bulk must be declared BEFORE /apply/{app_id} so FastAPI's
# in-order route matching doesn't treat "bulk" as a path-param.


async def _run_bulk_apply(app_ids: list[str]) -> None:
    """Process a list of app_ids sequentially in the background."""
    total = len(app_ids)
    succ = 0
    fail = 0
    broadcast({"type": "status", "message": f"Bulk apply: starting {total} job(s)…"})
    for i, app_id in enumerate(app_ids, start=1):
        ok, msg = await _execute_apply(app_id, label_prefix=f"[{i}/{total}]")
        if ok:
            succ += 1
        else:
            fail += 1
        broadcast({
            "type": "status",
            "message": f"[{i}/{total}] {'✓' if ok else '✗'} {msg}",
            "stats": await _dashboard_stats(),
        })
        # Brief breather between browser-based applies so we don't tank the host
        await asyncio.sleep(1.0)
    broadcast({
        "type": "done",
        "message": f"Bulk apply done — {succ} ok, {fail} failed",
        "stats": await _dashboard_stats(),
    })


@app.post("/apply/bulk")
async def apply_bulk(app_ids: list[str] = Form(...)):
    """Kick off bulk apply for the selected applications. Returns immediately."""
    # Sanity: dedupe and limit
    unique_ids = list(dict.fromkeys(app_ids))[:50]
    if not unique_ids:
        return RedirectResponse(url=f"/apply?message={quote('No applications selected')}", status_code=303)

    _spawn_bg(_run_bulk_apply(unique_ids))
    return RedirectResponse(
        url=f"/apply?message={quote(f'Bulk apply started for {len(unique_ids)} job(s) — watch the toasts')}",
        status_code=303,
    )


@app.post("/apply/{app_id}")
async def trigger_apply(app_id: str):
    """Apply tab action: send the email or run the ATS autofill for one app.

    Always redirects back to /apply — failures surface via the Failed bucket + toasts.
    """
    ok, msg = await _execute_apply(app_id)
    broadcast({
        "type": "done",
        "message": msg,
        "stats": await _dashboard_stats(),
    })
    return RedirectResponse(url=f"/apply?message={quote(msg)}", status_code=303)


@app.post("/test/apply-url")
async def test_apply_url(ats_url: str = Form(...)):
    """Apply to an arbitrary job URL on demand — useful for spot-testing the autofillers.

    Creates a synthetic Job + Application with status=approved and the given URL as ats_url,
    then runs the matching autofiller (or surfaces a helpful error if none matches).
    """
    import hashlib
    from autoapply.channel.router import _detect_ats

    ats_url = ats_url.strip()
    if not ats_url.startswith("http"):
        return RedirectResponse(url=f"/?message={quote('URL must start with http(s)')}", status_code=303)

    ats_label = _detect_ats(ats_url) or ""
    ats_kind = ats_label.split(":", 1)[-1] if ats_label else "unknown"

    settings = get_settings()
    db = get_db()
    ts = datetime.now(UTC).isoformat()
    job_doc = {
        "source": "test-url",
        "source_id": f"test-{hashlib.sha1(ats_url.encode()).hexdigest()[:12]}",
        "company": f"Test ({ats_kind})",
        "role": "Ad-hoc apply test",
        "jd_text": f"Ad-hoc test application to {ats_url}",
        "jd_url": ats_url,
        "jd_url_key": ats_url,
        "location": "",
        "remote": False,
        "raw_payload": {},
        "dedup_hash": hashlib.sha1(f"test-url|{ts}|{ats_url}".encode()).hexdigest(),
        "fit_score": 10.0,
        "fit_reasons": ["test-url"],
        "deal_breakers": [],
        "ingested_at": datetime.now(UTC),
    }
    job_result = await db[Job.collection].insert_one(job_doc)
    job_id = str(job_result.inserted_id)

    app_obj = Application(
        job_id=job_id,
        channel=Channel.ats,
        status=AppStatus.approved,
        ats_url=ats_url,
    )
    app_id = await app_obj.insert()

    broadcast({"type": "status", "message": f"Test apply to {ats_kind} — opening browser…"})

    # Try the apply right away; surface result via redirect message
    from autoapply.channel.autofill.base import get_filler
    from autoapply.channel.autofill.portal_apply import PortalApplyFailed, PortalLoginRequired

    filler = get_filler(ats_label)
    if filler is None:
        msg = f"No autofiller for {ats_kind!r} URL — created application in Apply tab for manual handling"
        broadcast({"type": "done", "message": msg, "stats": await _dashboard_stats()})
        return RedirectResponse(url=f"/apply?message={quote(msg)}", status_code=303)

    try:
        await filler.apply(app_id)
        broadcast({"type": "done", "message": f"Test apply submitted via {filler.name}", "stats": await _dashboard_stats()})
        return RedirectResponse(url=f"/apply?message={quote(f'Test apply submitted via {filler.name}!')}", status_code=303)
    except PortalLoginRequired as e:
        broadcast({"type": "done", "message": f"Login required for {filler.name}", "stats": await _dashboard_stats()})
        return RedirectResponse(url=f"/apply?message={quote(f'{filler.name}: {e}')}", status_code=303)
    except PortalApplyFailed as e:
        broadcast({"type": "done", "message": f"Test apply failed: {e}", "stats": await _dashboard_stats()})
        return RedirectResponse(url=f"/apply?message={quote(f'{filler.name}: {e}')}", status_code=303)
    except Exception as e:
        log.exception("test apply unexpected")
        return RedirectResponse(url=f"/apply?message={quote(f'Unexpected: {e}')}", status_code=303)


@app.post("/test/send-self")
async def create_test_send():
    """Create a self-test email application addressed to the user's own Gmail.

    Lets you verify the Gmail OAuth + composer + sender stack end-to-end without
    needing a real recruiter email.  Lands in the /apply tab as 'approved'.
    """
    import hashlib
    from autoapply.config import get_profile

    settings = get_settings()
    profile = get_profile()
    recipient = settings.gmail_user_email or profile.identity.email_primary

    if not recipient:
        return RedirectResponse(
            url=f"/?message={quote('No gmail_user_email or profile email set — cannot create test send')}",
            status_code=303,
        )

    db = get_db()
    # Synthetic job — unique dedup_hash via timestamp keeps it from colliding with future inserts
    ts = datetime.now(UTC).isoformat()
    fake_url = f"https://example.com/self-test/{ts}"
    job_doc = {
        "source": "self-test",
        "source_id": f"self-test-{ts}",
        "company": "Self-Test",
        "role": "Email send pipeline test",
        "jd_text": "Synthetic job to verify the email send flow end-to-end.",
        "jd_url": fake_url,
        "jd_url_key": fake_url,
        "location": profile.identity.location or "",
        "remote": True,
        "recruiter_name": profile.identity.full_name,
        "hr_email": recipient,
        "raw_payload": {},
        "dedup_hash": hashlib.sha1(f"self-test|{ts}".encode()).hexdigest(),
        "fit_score": 10.0,
        "fit_reasons": ["self-test job"],
        "deal_breakers": [],
        "ingested_at": datetime.now(UTC),
    }
    job_result = await db[Job.collection].insert_one(job_doc)
    job_id = str(job_result.inserted_id)

    app_obj = Application(
        job_id=job_id,
        channel=Channel.coldmail,
        status=AppStatus.approved,
        recipient=recipient,
        draft_subject=f"[Self-test] auto-apply email pipeline @ {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}",
        draft_body=(
            f"Hi {profile.identity.full_name},\n\n"
            "This is a self-test from your auto-apply pipeline.\n\n"
            "If you're reading this, the Gmail OAuth + composer + sender stack works end-to-end.\n\n"
            "If you'd rather I didn't follow up, reply STOP.\n"
        ),
    )
    await app_obj.insert()
    broadcast({"type": "status", "message": "Created self-test email application"})
    return RedirectResponse(
        url=f"/apply?message={quote('Self-test created — click Send email below to verify the stack')}",
        status_code=303,
    )


@app.post("/mark-submitted/{app_id}")
async def mark_submitted(app_id: str):
    """Manual override: mark an application as submitted (the user did it by hand)."""
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")
    a.status = AppStatus.submitted
    a.sent_at = datetime.now(UTC)
    a.error = None
    await a.save()
    broadcast({"type": "done", "message": "Marked as submitted"})
    return RedirectResponse(url="/apply", status_code=303)


@app.post("/skip/{app_id}")
async def skip(app_id: str):
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")
    a.status = AppStatus.skipped
    await a.save()
    return RedirectResponse(url="/", status_code=303)


@app.post("/agent")
async def agent(goal: str = Form(...)):
    run = await run_agent(goal)
    return RedirectResponse(url=f"/?last_agent={run.id}", status_code=303)


# === Blocklist ===============================================================


@app.get("/blocklist", response_class=HTMLResponse)
async def blocklist_page(request: Request):
    bl = _read_blocklist()
    return templates.TemplateResponse(
        request,
        "blocklist.html",
        {"companies": bl.companies, "domains": bl.domains, "notes": bl.notes},
    )


@app.post("/blocklist/add")
async def blocklist_add(kind: str = Form(...), value: str = Form(...)):
    bl = _read_blocklist()
    value = value.strip()
    if not value:
        return RedirectResponse(url="/blocklist", status_code=303)
    if kind == "company" and value not in bl.companies:
        bl.companies.append(value)
    elif kind == "domain" and value not in bl.domains:
        bl.domains.append(value)
    _write_blocklist(bl)
    return RedirectResponse(url="/blocklist", status_code=303)


@app.post("/blocklist/delete")
async def blocklist_delete(kind: str = Form(...), value: str = Form(...)):
    bl = _read_blocklist()
    if kind == "company":
        bl.companies = [c for c in bl.companies if c != value]
    elif kind == "domain":
        bl.domains = [d for d in bl.domains if d != value]
    _write_blocklist(bl)
    return RedirectResponse(url="/blocklist", status_code=303)


# === SSE endpoint ============================================================


@app.get("/events")
async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sse_clients.add(queue)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=4.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat doubles as a live stats refresh
                    stats = await _dashboard_stats()
                    sent_today = await _sent_today_count()
                    yield f"data: {json.dumps({'type': 'stats', 'stats': stats, 'sent_today': sent_today})}\n\n"
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# === Unapplied jobs page =====================================================


async def _list_unapplied() -> dict:
    db = get_db()
    job_coll = db[Job.collection]
    app_coll = db[Application.collection]

    # Failed applications
    failed_raw: list[dict] = []
    async for a in app_coll.find({"status": AppStatus.failed.value}):
        job = await job_coll.find_one({"_id": ObjectId(a["job_id"])})
        if job is None:
            continue
        failed_raw.append(
            {
                "id": str(a["_id"]),
                "company": job.get("company", ""),
                "role": job.get("role", ""),
                "channel": a.get("channel", ""),
                "score": job.get("fit_score") or 0.0,
                "error": a.get("error", ""),
                "job_url": job.get("jd_url") or "",
            }
        )
    failed_raw.sort(key=lambda x: x["score"], reverse=True)

    # Scored jobs with no application at all (not yet routed)
    unrouted_raw: list[dict] = []
    async for job in job_coll.find({"fit_score": {"$ne": None}}).sort("fit_score", -1).limit(100):
        existing = await app_coll.find_one({"job_id": str(job["_id"])})
        if existing:
            continue
        unrouted_raw.append(
            {
                "id": str(job["_id"]),
                "company": job.get("company", ""),
                "role": job.get("role", ""),
                "score": job.get("fit_score") or 0.0,
                "job_url": job.get("jd_url") or "",
            }
        )

    return {"failed": failed_raw, "unrouted": unrouted_raw}


@app.get("/unapplied", response_class=HTMLResponse)
async def unapplied_page(request: Request):
    data = await _list_unapplied()
    return templates.TemplateResponse(
        request,
        "unapplied.html",
        data,
    )


@app.post("/retry/{app_id}")
async def retry_application(app_id: str):
    """Reset a failed application back to approved so the Apply tab shows it again."""
    a = await Application.get(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="application not found")
    a.status = AppStatus.approved
    a.error = None
    await a.save()
    return RedirectResponse(url="/apply", status_code=303)


# === Portal credentials / session management =================================

_PORTALS = {
    "naukri":    ("Naukri",    "naukri_browser_dir",    "https://www.naukri.com/login",     True),
    "foundit":   ("Foundit",   "foundit_browser_dir",   "https://www.foundit.in/login",     True),
    "instahyre": ("Instahyre", "instahyre_browser_dir", "https://www.instahyre.com/login",  True),
    "linkedin":  ("LinkedIn",  "linkedin_browser_dir",  "https://www.linkedin.com/login",   False),  # bot-detection — manual only
}


def _portal_logged_in(profile_dir: Path) -> bool:
    """Heuristic: a non-empty profile dir (with Cookies file) means the user is logged in."""
    cookies = profile_dir / "Default" / "Cookies"
    return cookies.exists() and cookies.stat().st_size > 4096


def _gmail_status() -> dict:
    """Inspect Gmail OAuth state: secrets file present, token present + valid."""
    from autoapply.config import get_settings as _gs
    s = _gs()
    secrets_present = s.gmail_client_secrets.exists()
    token_present = s.gmail_token_path.exists()
    token_valid = False
    user_email = ""
    if token_present:
        try:
            from google.oauth2.credentials import Credentials
            from autoapply.sources.gmail_alerts import GMAIL_SCOPES
            creds = Credentials.from_authorized_user_file(str(s.gmail_token_path), GMAIL_SCOPES)
            token_valid = creds.valid or (creds.expired and bool(creds.refresh_token))
        except Exception:
            token_valid = False
    return {
        "secrets_present": secrets_present,
        "secrets_path": str(s.gmail_client_secrets),
        "token_present": token_present,
        "token_path": str(s.gmail_token_path),
        "token_valid": token_valid,
        "user_email": s.gmail_user_email or user_email,
    }


@app.get("/credentials", response_class=HTMLResponse)
async def credentials_page(request: Request):
    settings = get_settings()
    from autoapply.config import get_portals_config
    portals_cfg = get_portals_config()

    portals = []
    for key, (label, dir_attr, login_url, supports_auto) in _PORTALS.items():
        profile_dir: Path = getattr(settings, dir_attr)
        creds = getattr(portals_cfg, key, None) if hasattr(portals_cfg, key) else None
        portals.append(
            {
                "key": key,
                "label": label,
                "login_url": login_url,
                "profile_dir": str(profile_dir),
                "logged_in": _portal_logged_in(profile_dir),
                "supports_auto_login": supports_auto,
                "email": creds.email if creds else "",
                "has_password": bool(creds and creds.password),
            }
        )
    return templates.TemplateResponse(
        request,
        "credentials.html",
        {
            "portals": portals,
            "gmail": _gmail_status(),
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/portal/login/{portal}")
async def portal_login(portal: str):
    """Launch a headful browser for manual portal login.  Detached subprocess."""
    if portal not in _PORTALS:
        raise HTTPException(status_code=404, detail=f"Unknown portal: {portal}")

    script = Path(__file__).parent.parent.parent.parent / "scripts" / "setup_portal_login.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="setup_portal_login.py not found")

    subprocess.Popen([sys.executable, str(script), portal], start_new_session=True)
    return RedirectResponse(
        url=f"/credentials?message={quote(f'Browser opened for {portal} login. Log in then close the browser.')}",
        status_code=303,
    )


@app.post("/portal/credentials/{portal}")
async def portal_save_credentials(
    portal: str,
    email: str = Form(""),
    password: str = Form(""),
    action: str = Form("save"),
):
    """Save or clear email+password credentials for a portal."""
    from autoapply.config import get_portals_config, save_portals_config

    if portal not in _PORTALS:
        raise HTTPException(status_code=404, detail=f"Unknown portal: {portal}")
    if not hasattr(get_portals_config(), portal):
        raise HTTPException(status_code=400, detail=f"Portal {portal} doesn't accept stored credentials")

    cfg = get_portals_config()
    creds = getattr(cfg, portal)
    if action == "clear":
        creds.email = ""
        creds.password = ""
        msg = f"Cleared credentials for {portal}"
    else:
        if email:
            creds.email = email.strip()
        # Only overwrite password if user supplied one — otherwise keep existing
        if password:
            creds.password = password
        msg = f"Saved credentials for {portal}"
    save_portals_config(cfg)
    return RedirectResponse(url=f"/credentials?message={quote(msg)}", status_code=303)


@app.post("/portal/auto-login/{portal}")
async def portal_auto_login(portal: str):
    """Use stored email+password to log into the portal in a (headful) browser."""
    from autoapply.channel.autofill.portal_apply import (
        PortalLoginFailed,
        auto_login,
    )
    from autoapply.config import get_portals_config

    if portal not in _PORTALS:
        raise HTTPException(status_code=404, detail=f"Unknown portal: {portal}")

    settings = get_settings()
    cfg = get_portals_config()
    creds = getattr(cfg, portal, None)
    if creds is None or not creds.is_set:
        return RedirectResponse(
            url=f"/credentials?message={quote(f'No credentials saved for {portal} — set email and password first.')}",
            status_code=303,
        )

    dir_attr = _PORTALS[portal][1]
    profile_dir: Path = getattr(settings, dir_attr)

    broadcast({"type": "status", "message": f"Auto-logging in to {portal}…"})
    try:
        await auto_login(
            portal=portal,
            email=creds.email,
            password=creds.password,
            profile_dir=profile_dir,
            headless=False,  # show the browser so captcha/2FA can be solved
        )
        broadcast({"type": "done", "message": f"Logged in to {portal}"})
        return RedirectResponse(
            url=f"/credentials?message={quote(f'Successfully logged in to {portal}!')}",
            status_code=303,
        )
    except PortalLoginFailed as e:
        broadcast({"type": "done", "message": f"{portal} login failed: {e}"})
        return RedirectResponse(
            url=f"/credentials?message={quote(f'{portal} login failed: {e}')}",
            status_code=303,
        )


@app.post("/gmail/setup")
async def gmail_setup():
    """Launch the Gmail OAuth setup script in a detached subprocess."""
    script = Path(__file__).parent.parent.parent.parent / "scripts" / "setup_gmail_oauth.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="setup_gmail_oauth.py not found")
    settings = get_settings()
    if not settings.gmail_client_secrets.exists():
        return RedirectResponse(
            url=f"/credentials?message={quote(f'gmail_credentials.json not found at {settings.gmail_client_secrets} — download OAuth Desktop credentials from Google Cloud Console first.')}",
            status_code=303,
        )
    subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    return RedirectResponse(
        url=f"/credentials?message={quote('Gmail OAuth flow started in a browser window. Complete sign-in to save the token.')}",
        status_code=303,
    )
