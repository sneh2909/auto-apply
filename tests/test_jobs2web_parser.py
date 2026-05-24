"""Tests for Jobs2Web talent-community email parser and ingest filters."""

from __future__ import annotations

from autoapply.config import Blocklist, Profile, Target, Identity, Career, Skills
from autoapply.sources.job_filter import filter_job_records, role_matches_profile
from autoapply.sources.parsers.jobs2web import Jobs2WebParser

DELOITTE_SAMPLE = """
New jobs posted from southasiacareers.deloitte.com

Your Job Alert matched the following jobs at southasiacareers.deloitte.com.

Jobs
Assistant Manager | Engineering Foundry | Hyderabad - Hyderabad, IN
T&T| Azure Data Engineer | Manager | Bengaluru - Bengaluru, IN
Senior Consultant | SAP MM | Noida | SAP - Noida, IN
"""

MAHINDRA_SAMPLE = """
New jobs posted from jobs.mahindracareers.com

Your Job Agent (Data Scientist, Mumbai - Worli IN) matched the following jobs at jobs.mahindracareers.com.

Jobs
Digitization & Analytics Manager - Mumbai A.O, MUM-KND-AFS(AD), IN
AI Engineer - Mumbai - Worli, Mumbai - Worli, IN
General Manager - FP&A - Mumbai - Worli, Mumbai - Worli, IN
"""


def _minimal_profile(**kwargs) -> Profile:
    target = Target(
        roles=["Machine Learning Engineer", "AI Engineer", "Data Scientist"],
        on_site_cities_ok=["Bangalore", "Hyderabad", "Pune"],
        remote_ok=True,
    )
    if "target" in kwargs:
        target = kwargs.pop("target")
    return Profile(
        identity=Identity(full_name="Test", email_primary="t@example.com"),
        career=Career(),
        target=target,
        skills=Skills(),
        **kwargs,
    )


def test_jobs2web_parses_deloitte_digest():
    parser = Jobs2WebParser()
    msg = {"id": "1"}
    records = parser.parse(
        msg,
        "",
        DELOITTE_SAMPLE,
    )
    assert len(records) >= 2
    assert any("Azure Data Engineer" in r.role for r in records)
    assert records[0].company == "Deloitte"
    assert records[0].raw_payload.get("career_site") == "southasiacareers.deloitte.com"


def test_jobs2web_parses_mahindra_ai_engineer():
    parser = Jobs2WebParser()
    records = parser.parse({"id": "2"}, "", MAHINDRA_SAMPLE)
    roles = [r.role for r in records]
    assert any("AI Engineer" in r for r in roles)
    assert any(r.company == "Mahindra" for r in records)


def test_jobs2web_matches_sender():
    parser = Jobs2WebParser()
    assert parser.matches_message(
        {},
        "mahindra-jobnotification@noreply.jobs2web.com",
        "New jobs",
        "",
        "",
    )


def test_filter_keeps_ml_role_in_allowed_city():
    profile = _minimal_profile()
    bl = Blocklist()
    from autoapply.sources.base import JobRecord

    records = [
        JobRecord(
            source="gmail:jobs2web",
            source_id="1",
            company="Mahindra",
            role="AI Engineer",
            jd_text="x",
            location="Mumbai - Worli",
        ),
        JobRecord(
            source="gmail:jobs2web",
            source_id="2",
            company="Deloitte",
            role="Senior Consultant | SAP MM",
            jd_text="x",
            location="Noida",
        ),
    ]
    profile.identity.willing_to_relocate = ["Mumbai", "Bangalore", "Hyderabad", "Pune"]
    kept = filter_job_records(records, profile=profile, blocklist=bl)
    roles = {r.role for r in kept}
    assert any("AI Engineer" in r for r in roles)
    assert not any("SAP MM" in r for r in roles)


def test_filter_blocks_blocklist_company():
    profile = _minimal_profile()
    bl = Blocklist(companies=["NoBroker"])
    from autoapply.sources.base import JobRecord

    records = [
        JobRecord(
            source="test",
            source_id="x",
            company="NoBroker",
            role="Machine Learning Engineer",
            jd_text="x",
            location="Bangalore",
        ),
    ]
    assert filter_job_records(records, profile=profile, blocklist=bl) == []


def test_role_match_ai_engineer():
    profile = _minimal_profile()
    assert role_matches_profile("AI Engineer", profile)
