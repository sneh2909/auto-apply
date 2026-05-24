from __future__ import annotations

from bson import ObjectId

from autoapply.db.models import Job
from autoapply.score import scorer
from autoapply.sources.base import JobRecord
from autoapply.sources.browser_agent.agent import GeminiJobAgent


def test_job_record_stores_normalized_url_key():
    job = JobRecord(
        source="test",
        source_id="1",
        company="Acme",
        role="ML Engineer",
        jd_text="Build models",
        jd_url="HTTPS://Example.com/jobs/123/?utm_source=x&ref=mail&b=2&a=1#section",
    ).to_job()

    assert job.jd_url_key == "https://example.com/jobs/123?a=1&b=2"


async def test_score_and_persist_updates_by_object_id(monkeypatch):
    job_id = ObjectId()
    captured: dict = {}

    async def fake_score_job(job):
        return {"score": 8.0, "reasons": ["match"], "deal_breakers": []}

    class FakeCollection:
        async def update_one(self, filt, update):
            captured["filter"] = filt
            captured["update"] = update

    class FakeDb:
        def __getitem__(self, name):
            assert name == Job.collection
            return FakeCollection()

    monkeypatch.setattr(scorer, "score_job", fake_score_job)
    monkeypatch.setattr(scorer, "get_db", lambda: FakeDb())

    job = Job(
        _id=str(job_id),
        source="test",
        source_id="1",
        company="Acme",
        role="ML Engineer",
        jd_text="Build models",
        dedup_hash="abc",
    )
    await scorer.score_and_persist(job)

    assert captured["filter"] == {"_id": job_id}
    assert captured["update"]["$set"]["fit_score"] == 8.0


async def test_browser_agent_save_job_persists_immediately(monkeypatch):
    persisted: list[JobRecord] = []

    async def fake_upsert_jobs(records):
        persisted.extend(records)
        return len(records), 0

    monkeypatch.setattr("autoapply.sources.browser_agent.agent.upsert_jobs", fake_upsert_jobs)

    agent = GeminiJobAgent(browser=None, portal="naukri")
    result = await agent._tool_save(
        {
            "role": "ML Engineer",
            "company": "Acme",
            "location": "Bangalore",
            "jd_url": "https://example.com/jobs/1",
            "jd_text": "Build ranking models",
        }
    )

    assert "persisted" in result
    assert len(persisted) == 1
    assert persisted[0].jd_url == "https://example.com/jobs/1"
