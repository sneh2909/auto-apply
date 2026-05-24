"""Offline test for the Naukri alert parser."""

from autoapply.sources.parsers.naukri import NaukriParser


def test_parser_extracts_three_jobs(fixtures_dir):
    html = (fixtures_dir / "naukri_sample.html").read_text()
    records = NaukriParser().parse(message={"id": "test-msg"}, html=html, text="")

    assert len(records) == 3, f"expected 3 jobs, got {len(records)}: {[r.role for r in records]}"

    roles = {r.role for r in records}
    assert "Senior Machine Learning Engineer" in roles
    assert "Applied Scientist (NLP)" in roles
    assert "MLOps Engineer" in roles


def test_parser_finds_company_and_location(fixtures_dir):
    html = (fixtures_dir / "naukri_sample.html").read_text()
    records = NaukriParser().parse(message={"id": "test-msg"}, html=html, text="")

    ml = next(r for r in records if "Machine Learning" in r.role)
    assert "Acme Corp" in ml.company
    assert ml.location is not None and "engaluru" in ml.location  # tolerant of Bengaluru/Bangalore variants


def test_parser_detects_remote(fixtures_dir):
    html = (fixtures_dir / "naukri_sample.html").read_text()
    records = NaukriParser().parse(message={"id": "test-msg"}, html=html, text="")

    mlops = next(r for r in records if "MLOps" in r.role)
    assert mlops.remote is True or (mlops.location and "remote" in mlops.location.lower())


def test_dedup_hash_is_stable(fixtures_dir):
    html = (fixtures_dir / "naukri_sample.html").read_text()
    a = NaukriParser().parse(message={"id": "m1"}, html=html, text="")
    b = NaukriParser().parse(message={"id": "m2"}, html=html, text="")
    assert {r.dedup_hash for r in a} == {r.dedup_hash for r in b}


def test_handles_empty_input():
    records = NaukriParser().parse(message={"id": "empty"}, html="", text="")
    assert records == []
