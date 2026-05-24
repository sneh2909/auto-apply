"""Tests for scrape_utils query building and text extraction."""

from __future__ import annotations

from autoapply.config import Career, Identity, Profile, Skills, Target
from autoapply.sources.scrape_utils import (
    build_linkedin_content_queries,
    build_role_location_combos,
    extract_email,
    extract_role,
    looks_like_hiring,
    profile_locations,
)


def _profile() -> Profile:
    return Profile(
        identity=Identity(
            full_name="Test",
            email_primary="t@example.com",
            willing_to_relocate=["Mumbai", "Bangalore"],
        ),
        career=Career(),
        target=Target(
            roles=["Machine Learning Engineer", "AI Engineer"],
            on_site_cities_ok=["Bangalore", "Hyderabad"],
            remote_ok=True,
        ),
        skills=Skills(),
    )


def test_build_linkedin_queries_includes_role_and_city():
    queries = build_linkedin_content_queries(_profile())
    assert any("Machine Learning Engineer" in q and "Bangalore" in q for q in queries)
    assert any("remote" in q.lower() for q in queries)


def test_profile_locations_includes_willing_to_relocate():
    locs = profile_locations(_profile())
    assert "Mumbai" in locs
    assert "Remote" in locs


def test_extract_email_from_hiring_post():
    text = "We're hiring a Senior ML Engineer at Acme! Email priya.recruiter@acme.ai"
    assert extract_email(text) == "priya.recruiter@acme.ai"
    assert looks_like_hiring(text)
    assert "ML Engineer" in extract_role(text, fallback="")


def test_role_location_combos():
    combos = build_role_location_combos(_profile())
    assert ("Machine Learning Engineer", "Bangalore") in combos
