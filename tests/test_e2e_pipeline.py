"""End-to-end pipeline tests with mocked I/O.

Tests the full path:
  JobRecord → upsert_jobs → score_and_persist → decide → route_once → Application
  → approve → send_application (email) / autofill (ATS)

External dependencies (MongoDB, LLM APIs, Gmail, Playwright) are all mocked so
tests run offline in CI.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autoapply.db.models import Application, AppStatus, Channel, Job
from autoapply.score.llm import _parse_json_robust
from autoapply.sources.base import JobRecord, _url_key


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_job_raw(
    *,
    role: str = "ML Engineer",
    company: str = "Acme AI",
    jd_url: str = "https://www.naukri.com/job-listings-ml-engineer-acme-1234",
    fit_score: float | None = None,
    hr_email: str | None = None,
    source: str = "scrape:naukri",
) -> dict:
    return {
        "_id": __import__("bson").ObjectId(),
        "source": source,
        "source_id": "test-001",
        "company": company,
        "role": role,
        "jd_text": "We are looking for an ML Engineer with Python and PyTorch skills.",
        "jd_url": jd_url,
        "jd_url_key": _url_key(jd_url),
        "location": "Bangalore",
        "remote": False,
        "fit_score": fit_score,
        "fit_reasons": ["strong skill match"],
        "deal_breakers": [],
        "hr_email": hr_email,
        "dedup_hash": "abc123",
        "recruiter_name": None,
        "raw_payload": {},
        "ingested_at": datetime.now(UTC),
    }


def _make_app_raw(
    *,
    job_id: str,
    channel: str = "ats",
    status: str = "queued",
    ats_url: str | None = None,
    recipient: str | None = None,
) -> dict:
    return {
        "_id": __import__("bson").ObjectId(),
        "job_id": job_id,
        "channel": channel,
        "status": status,
        "ats_url": ats_url,
        "recipient": recipient,
        "draft_subject": None,
        "draft_body": None,
        "error": None,
        "sender_account": "",
        "sent_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


# ── URL normalisation ─────────────────────────────────────────────────────────

def test_url_key_strips_utm():
    url = "https://www.naukri.com/job?id=123&utm_source=email&utm_campaign=jobs"
    key = _url_key(url)
    assert "utm_source" not in key
    assert "id=123" in key


def test_url_key_sorts_query_params():
    url1 = "https://boards.greenhouse.io/acme/jobs/1?a=1&b=2"
    url2 = "https://boards.greenhouse.io/acme/jobs/1?b=2&a=1"
    assert _url_key(url1) == _url_key(url2)


def test_url_key_strips_trailing_slash():
    assert _url_key("https://example.com/jobs/") == _url_key("https://example.com/jobs")


# ── JSON parsing robustness ───────────────────────────────────────────────────

def test_parse_json_robust_empty():
    assert _parse_json_robust("") == {}


def test_parse_json_robust_markdown_fence():
    text = '```json\n{"score": 8.5, "reasons": ["good"]}\n```'
    out = _parse_json_robust(text)
    assert out["score"] == 8.5


def test_parse_json_robust_plain_json():
    out = _parse_json_robust('{"score": 7.0}')
    assert out["score"] == 7.0


def test_parse_json_robust_regex_fallback():
    out = _parse_json_robust('Some preamble {"score": 6.0} some suffix')
    assert out["score"] == 6.0


def test_parse_json_robust_garbage():
    assert _parse_json_robust("not json at all!!!") == {}


# ── JobRecord dedup hash ──────────────────────────────────────────────────────

def test_dedup_hash_stable():
    rec = JobRecord(
        source="test",
        source_id="1",
        company="Acme AI",
        role="ML Engineer",
        jd_text="We need Python and PyTorch skills.",
    )
    h1 = rec.dedup_hash
    h2 = rec.dedup_hash
    assert h1 == h2


def test_dedup_hash_differs_by_company():
    base = dict(source="t", source_id="1", role="ML Eng", jd_text="PyTorch Python")
    r1 = JobRecord(company="Acme", **base)
    r2 = JobRecord(company="Beta", **base)
    assert r1.dedup_hash != r2.dedup_hash


# ── Channel router ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_router_detects_naukri():
    from autoapply.channel.router import _detect_ats
    url = "https://www.naukri.com/job-listings-ml-engineer-acme-1234"
    assert _detect_ats(url) == "A:naukri"


@pytest.mark.asyncio
async def test_router_detects_greenhouse():
    from autoapply.channel.router import _detect_ats
    url = "https://boards.greenhouse.io/acme/jobs/123456"
    assert _detect_ats(url) == "A:greenhouse"


@pytest.mark.asyncio
async def test_router_skip_low_score():
    from autoapply.channel.router import decide
    from autoapply.db.models import Job

    raw = _make_job_raw(fit_score=3.0)
    job = Job.model_validate(raw)

    with patch("autoapply.channel.router.get_blocklist") as mock_bl, \
         patch("autoapply.channel.router._already_applied", new_callable=AsyncMock, return_value=False):
        from autoapply.config import Blocklist
        mock_bl.return_value = Blocklist()
        letter, reason = await decide(job)

    assert letter == "skip"
    assert "threshold" in reason


@pytest.mark.asyncio
async def test_router_routes_naukri_to_ats():
    from autoapply.channel.router import decide
    from autoapply.db.models import Job

    raw = _make_job_raw(fit_score=8.0)
    job = Job.model_validate(raw)

    with patch("autoapply.channel.router.get_blocklist") as mock_bl, \
         patch("autoapply.channel.router._already_applied", new_callable=AsyncMock, return_value=False):
        from autoapply.config import Blocklist
        mock_bl.return_value = Blocklist()
        letter, reason = await decide(job)

    assert letter == "A"
    assert "naukri" in reason


@pytest.mark.asyncio
async def test_router_routes_email_when_hr_email_present():
    from autoapply.channel.router import decide
    from autoapply.db.models import Job

    raw = _make_job_raw(
        fit_score=8.0,
        jd_url="https://careers.example.com/jobs/ml-eng",
        hr_email="hr@example.com",
    )
    job = Job.model_validate(raw)

    with patch("autoapply.channel.router.get_blocklist") as mock_bl, \
         patch("autoapply.channel.router._already_applied", new_callable=AsyncMock, return_value=False):
        from autoapply.config import Blocklist
        mock_bl.return_value = Blocklist()
        letter, reason = await decide(job)

    assert letter == "B"


# ── Scorer ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scorer_always_apply_company():
    from autoapply.score.scorer import score_and_persist

    raw = _make_job_raw(company="DeepMind")

    with patch("autoapply.score.scorer.get_profile") as mock_profile, \
         patch("autoapply.score.scorer.get_db") as mock_db:
        profile = MagicMock()
        profile.always_apply = ["deepmind"]
        profile.target.deal_breakers = []
        mock_profile.return_value = profile

        mock_coll = AsyncMock()
        mock_db.return_value.__getitem__.return_value = mock_coll

        job = Job.model_validate(raw)
        updated_job = await score_and_persist(job)

    assert updated_job.fit_score == 10.0
    mock_coll.update_one.assert_called_once()


# ── Upsert jobs ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_no_conflict():
    from autoapply.sources.base import upsert_jobs

    rec = JobRecord(
        source="test",
        source_id="u1",
        company="WidgetCo",
        role="AI Engineer",
        jd_text="LLM expertise required",
        jd_url="https://widgetco.com/jobs/ai-eng",
    )

    mock_result = MagicMock()
    mock_result.upserted_id = __import__("bson").ObjectId()

    with patch("autoapply.sources.base.get_db") as mock_db:
        mock_coll = AsyncMock()
        mock_coll.update_one.return_value = mock_result
        mock_db.return_value.__getitem__.return_value = mock_coll

        inserted, skipped = await upsert_jobs([rec])

    assert inserted == 1
    assert skipped == 0

    # Verify $set and $setOnInsert don't overlap
    call_args = mock_coll.update_one.call_args
    update_doc = call_args[0][1]
    if "$set" in update_doc and "$setOnInsert" in update_doc:
        set_keys = set(update_doc["$set"].keys())
        insert_keys = set(update_doc["$setOnInsert"].keys())
        assert set_keys.isdisjoint(insert_keys), "Fields must not appear in both $set and $setOnInsert"


# ── Email send path ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_application_updates_status():
    from autoapply.channel.email.sender import send_application

    raw_app = _make_app_raw(
        job_id=str(__import__("bson").ObjectId()),
        channel="email",
        status="approved",
        recipient="hr@example.com",
    )
    app = Application.model_validate(raw_app)
    app.draft_subject = "[Application] ML Engineer — Sneh"
    app.draft_body = "Hi, I'd love to apply.\n\nIf you'd rather I didn't follow up, reply STOP."

    with patch("autoapply.channel.email.sender.GmailClient") as MockGmail, \
         patch("autoapply.channel.email.sender.get_settings") as mock_settings, \
         patch("autoapply.db.models.Application.save", new_callable=lambda: lambda self: AsyncMock(return_value=None)()):

        settings = MagicMock()
        settings.gmail_user_email = "me@gmail.com"
        settings.gmail_sender_name = "Sneh Shah"
        settings.resume_pdf = Path("/nonexistent/resume.pdf")
        settings.gmail_label_applied_out = "Applied/Sent"
        mock_settings.return_value = settings

        gmail_instance = MagicMock()
        gmail_instance.label_id.return_value = "label123"
        gmail_instance.send_message.return_value = {"id": "msg001"}
        MockGmail.return_value = gmail_instance

        await send_application(app)

    assert app.status == AppStatus.sent
    assert app.sent_at is not None


# ── Portal apply error handling ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portal_apply_raises_login_required():
    from autoapply.channel.autofill.portal_apply import PortalLoginRequired, apply_on_portal

    # Build a fake BrowserSession that looks like it was redirected to login
    class FakeBrowserSession:
        def __init__(self, **kwargs):
            self.url = "https://www.naukri.com/login?redirectUrl=/some-job"
            self.page = MagicMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def navigate(self, url, **kwargs):
            return ""

        async def get_text(self, **kwargs):
            return ""

    with patch("autoapply.sources.browser_agent.browser.BrowserSession", FakeBrowserSession):
        with pytest.raises(PortalLoginRequired):
            await apply_on_portal(
                portal="naukri",
                job_url="https://www.naukri.com/job-listings-ml-engineer-acme-1234",
                profile_dir=Path("/tmp/fake-profile"),
                qa_bank={},
                resume_pdf=None,
            )


# ── OpenAI-compatible client ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_client_json_mode():
    from autoapply.score.llm import OpenAIClient

    client = OpenAIClient(model="gpt-4o-mini", api_key="test-key")

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"score": 8.0, "reasons": ["good match"]}'

    with patch("openai.AsyncOpenAI") as MockClient:
        instance = AsyncMock()
        instance.chat.completions.create.return_value = mock_response
        MockClient.return_value = instance

        result = await client.chat_json(
            [{"role": "user", "content": "score this job"}],
            temperature=0.2,
        )

    assert result["score"] == 8.0


def test_get_scorer_llm_openai():
    from autoapply.score.llm import OpenAIClient, get_scorer_llm

    with patch("autoapply.score.llm.get_settings") as mock_settings:
        s = MagicMock()
        s.scorer_provider = "openai"
        s.scorer_model = "openai/gpt-4o-mini"
        s.scorer_api_key = None
        s.openai_api_key = MagicMock()
        s.openai_api_key.get_secret_value.return_value = "sk-test"
        s.openai_base_url = "https://api.openai.com/v1"
        mock_settings.return_value = s

        llm = get_scorer_llm()

    assert isinstance(llm, OpenAIClient)
    assert llm.base_url == "https://api.openai.com/v1"


def test_get_scorer_llm_ollama():
    from autoapply.score.llm import OpenAIClient, get_scorer_llm

    with patch("autoapply.score.llm.get_settings") as mock_settings:
        s = MagicMock()
        s.scorer_provider = "ollama"
        s.scorer_model = "ollama/llama3.2"
        s.scorer_api_key = None
        s.openai_api_key = None
        s.openai_base_url = "http://localhost:11434/v1"
        mock_settings.return_value = s

        llm = get_scorer_llm()

    assert isinstance(llm, OpenAIClient)
    assert "11434" in llm.base_url
    assert llm.api_key == "ollama"
