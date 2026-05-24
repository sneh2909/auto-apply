"""Tests for LLM extract helpers (no live LLM calls)."""

from __future__ import annotations

from autoapply.sources.llm_extract import (
    looks_personal_email,
    normalize_obfuscated_email,
)


def test_obfuscated_email_decoded():
    text = "Reach out to priya.sharma [at] acme [dot] ai for this ML Engineer role."
    assert normalize_obfuscated_email(text) == "priya.sharma@acme.ai"


def test_rejects_linkedin_platform_email():
    assert not looks_personal_email("noreply@linkedin.com")
    assert looks_personal_email("recruiter@startup.io")
