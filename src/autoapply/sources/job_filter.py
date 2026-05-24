"""Filter job records by profile (role + location) and blocklist before persistence."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from rapidfuzz import fuzz

from autoapply.config import Blocklist, Profile, get_blocklist, get_profile
from autoapply.sources.base import JobRecord

log = logging.getLogger(__name__)

_CITY_ALIASES: dict[str, str] = {
    "bengaluru": "bangalore",
    "bangalore": "bangalore",
    "bombay": "mumbai",
    "mumbai": "mumbai",
    "gurgaon": "gurgaon",
    "gurugram": "gurgaon",
    "delhi": "delhi",
    "new delhi": "delhi",
    "noida": "noida",
    "hyderabad": "hyderabad",
    "pune": "pune",
    "chennai": "chennai",
    "ahmedabad": "ahmedabad",
    "remote": "remote",
    "wfh": "remote",
}

_ML_ROLE_PHRASES = (
    "machine learning",
    "ml engineer",
    "mle",
    "mlops",
    "ai engineer",
    "applied scientist",
    "deep learning",
    "data scientist",
    "research scientist",
    "nlp",
    "computer vision",
    "generative ai",
    "llm engineer",
)


def _norm_city(name: str) -> str:
    key = (name or "").strip().lower()
    return _CITY_ALIASES.get(key, key)


def _location_tokens(location: str | None) -> set[str]:
    if not location:
        return set()
    raw = location.replace("|", " ").replace(",", " ").replace("-", " ")
    tokens: set[str] = set()
    for part in raw.split():
        part = part.strip()
        if len(part) < 3:
            continue
        tokens.add(_norm_city(part))
    for alias, canonical in _CITY_ALIASES.items():
        if alias in location.lower():
            tokens.add(canonical)
    return tokens


def _allowed_cities(profile: Profile) -> set[str]:
    cities = set(profile.target.on_site_cities_ok)
    cities.update(profile.identity.willing_to_relocate)
    return {_norm_city(c) for c in cities if c}


def role_matches_profile(role: str, profile: Profile) -> bool:
    """True if the role title is similar to any target role or common ML/AI titles."""
    rl = (role or "").lower()
    if not rl:
        return False
    for target in profile.target.roles:
        if fuzz.partial_ratio(target.lower(), rl) >= 58:
            return True
        # All significant words from a multi-word target appear in the posting title.
        words = [w for w in target.lower().split() if len(w) > 3]
        if len(words) >= 2 and all(w in rl for w in words):
            return True
    return any(phrase in rl for phrase in _ML_ROLE_PHRASES)


def location_matches_profile(
    location: str | None,
    *,
    remote: bool,
    profile: Profile,
) -> bool:
    if remote and profile.target.remote_ok:
        return True
    if not location:
        # Keep unknown-location jobs for scoring; scrapers/Gmail may omit city.
        return True
    loc_l = location.lower()
    if "remote" in loc_l and profile.target.remote_ok:
        return True
    allowed = _allowed_cities(profile)
    if not allowed:
        return True
    tokens = _location_tokens(location)
    return bool(tokens & allowed)


def is_blocked_record(record: JobRecord, blocklist: Blocklist) -> bool:
    company_l = (record.company or "").lower()
    for blocked in blocklist.companies:
        if blocked.lower() in company_l:
            return True
    url = record.jd_url or ""
    if url:
        host = urlparse(url).netloc.lower()
        for domain in blocklist.domains:
            if domain.lower() in host:
                return True
    # Career-site domain in raw payload (jobs2web emails).
    site = (record.raw_payload.get("career_site") or "").lower()
    for domain in blocklist.domains:
        if domain.lower() in site:
            return True
    return False


def filter_job_records(
    records: list[JobRecord],
    *,
    profile: Profile | None = None,
    blocklist: Blocklist | None = None,
) -> list[JobRecord]:
    """Drop jobs outside profile role/location or on the blocklist."""
    if not records:
        return []
    profile = profile or get_profile()
    blocklist = blocklist or get_blocklist()

    kept: list[JobRecord] = []
    dropped_role = dropped_loc = dropped_block = 0
    for rec in records:
        if is_blocked_record(rec, blocklist):
            dropped_block += 1
            log.debug("filtered (blocklist): %s @ %s", rec.role, rec.company)
            continue
        if not role_matches_profile(rec.role, profile):
            dropped_role += 1
            log.debug("filtered (role): %s @ %s", rec.role, rec.company)
            continue
        if not location_matches_profile(rec.location, remote=rec.remote, profile=profile):
            dropped_loc += 1
            log.debug("filtered (location): %s @ %s — %s", rec.role, rec.company, rec.location)
            continue
        kept.append(rec)

    if dropped_role or dropped_loc or dropped_block:
        log.info(
            "job filter: kept=%d dropped role=%d location=%d blocklist=%d",
            len(kept),
            dropped_role,
            dropped_loc,
            dropped_block,
        )
    return kept
