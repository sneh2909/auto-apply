"""Tests for Jobs2Web post line parsing used by linkedin_posts (shared pattern)."""

from __future__ import annotations

from autoapply.sources.scrapers.linkedin_posts import LinkedInPostsScraper

MAHINDRA_LINE = """
AI Engineer - Mumbai - Worli, Mumbai - Worli, IN
General Manager - FP&A - Mumbai - Worli, Mumbai - Worli, IN
"""


def test_post_to_record_extracts_email():
    raw = {
        "urn": "urn:li:activity:123",
        "author": "Recruiter Name",
        "subtitle": "Talent @ Mahindra",
        "text": (
            "We're hiring an AI Engineer in Mumbai! "
            "Send CV to hr.team@mahindra.com — join our data science team."
        ),
        "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:123/",
    }
    rec = LinkedInPostsScraper._post_to_record("AI Engineer hiring Mumbai", raw, 25)
    assert rec is not None
    assert rec.hr_email == "hr.team@mahindra.com"
    assert rec.company == "Mahindra"
    assert "AI Engineer" in rec.role or "hiring" in rec.role.lower()
