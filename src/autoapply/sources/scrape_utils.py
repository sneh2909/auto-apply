"""Shared helpers for browser scrapers: query building and text extraction."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from autoapply.config import Profile

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SKIP_EMAIL_DOMAINS = (
    "linkedin.com",
    "noreply",
    "no-reply",
    "donotreply",
    "example.com",
    "sentry.io",
)

_HIRING_TOKENS = (
    "hiring",
    "we're hiring",
    "we are hiring",
    "open role",
    "open position",
    "looking for",
    "looking to hire",
    "join our team",
    "now hiring",
    "actively hiring",
    "send your resume",
    "send your cv",
    "dm me",
    "apply now",
    "referral",
)

_ROLE_PATTERNS = (
    re.compile(r"hiring (?:a |an |for a |for an )?([A-Z][\w\s/+\-]{2,70}?)\s*(?:[,.\n]|at )", re.IGNORECASE),
    re.compile(r"looking for (?:a |an )?([A-Z][\w\s/+\-]{2,70}?)\s*(?:[,.\n]|to )", re.IGNORECASE),
    re.compile(r"\bopen (?:role|position)\s*(?:for|:)\s*([A-Z][\w\s/+\-]{2,70}?)\s*(?:[,.\n]|at )", re.IGNORECASE),
    re.compile(r"\brole:\s*([A-Z][\w\s/+\-]{2,70}?)\s*(?:[,.\n])", re.IGNORECASE),
)

_COMPANY_AT_RE = re.compile(r"\bat\s+([A-Z][\w&.\- ]{1,50}?)(?:\s*(?:\(|\||·|\.|,|$))")

_INDIA_CITIES = (
    "bangalore",
    "bengaluru",
    "hyderabad",
    "pune",
    "mumbai",
    "delhi",
    "ncr",
    "gurgaon",
    "gurugram",
    "noida",
    "chennai",
    "kolkata",
    "ahmedabad",
    "worli",
    "remote",
)

_LINKEDIN_JOB_URL_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)",
    re.IGNORECASE,
)


def profile_roles(profile: Profile, *, limit: int = 5) -> list[str]:
    return (profile.target.roles or ["Machine Learning Engineer"])[:limit]


def profile_locations(profile: Profile, *, limit: int = 5) -> list[str]:
    locs = list(profile.target.on_site_cities_ok or ["Bangalore"])
    locs.extend(profile.identity.willing_to_relocate or [])
    if profile.target.remote_ok:
        locs.append("Remote")
    # Preserve order, dedupe
    seen: set[str] = set()
    out: list[str] = []
    for loc in locs:
        key = loc.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(loc.strip())
        if len(out) >= limit:
            break
    return out


def build_linkedin_content_queries(profile: Profile) -> list[str]:
    """Content-search queries: role + hiring + city (from profile)."""
    queries: list[str] = []
    seen: set[str] = set()
    for role in profile_roles(profile, limit=4):
        short = role.split(",")[0].strip()
        for loc in profile_locations(profile, limit=4):
            if loc.lower() == "remote":
                q = f'"{short}" hiring remote India'
            else:
                q = f'"{short}" hiring {loc}'
            key = q.lower()
            if key not in seen:
                seen.add(key)
                queries.append(q)
    return queries or ['"Machine Learning Engineer" hiring Bangalore']


def build_role_location_combos(profile: Profile) -> list[tuple[str, str]]:
    combos: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for role in profile_roles(profile):
        for loc in profile_locations(profile):
            key = (role.lower(), loc.lower())
            if key not in seen:
                seen.add(key)
                combos.append((role, loc))
    return combos


def looks_like_hiring(text: str) -> bool:
    lc = text.lower()
    return any(t in lc for t in _HIRING_TOKENS)


def extract_email(text: str) -> str | None:
    for m in _EMAIL_RE.findall(text):
        lc = m.lower()
        if any(skip in lc for skip in _SKIP_EMAIL_DOMAINS):
            continue
        return m
    return None


def extract_all_emails(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_RE.findall(text):
        lc = m.lower()
        if any(skip in lc for skip in _SKIP_EMAIL_DOMAINS):
            continue
        if lc not in seen:
            seen.add(lc)
            out.append(m)
    return out


def extract_role(text: str, *, fallback: str = "") -> str:
    for pat in _ROLE_PATTERNS:
        m = pat.search(text)
        if m:
            role = re.sub(r"\s+", " ", m.group(1)).strip()
            role = re.sub(r"\s+(in|at|for|with|to)\s*$", "", role, flags=re.IGNORECASE).strip()
            if 4 <= len(role) <= 80:
                return role
    return fallback


def extract_company(subtitle: str, text: str) -> str | None:
    for chunk in (subtitle, text):
        if not chunk:
            continue
        for marker in (" @ ", " at "):
            if marker in chunk:
                tail = chunk.split(marker, 1)[1]
                cand = re.split(r"[·|()|·,.\n]", tail, maxsplit=1)[0].strip()
                if 2 <= len(cand) <= 60:
                    return cand
        m = _COMPANY_AT_RE.search(chunk)
        if m:
            return m.group(1).strip()
    return None


def extract_location(text: str) -> str | None:
    lc = text.lower()
    for city in _INDIA_CITIES:
        if city in lc:
            return city.title() if city != "ncr" else "NCR"
    return None


def extract_linkedin_job_urls(text: str) -> list[str]:
    return [
        f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
        for m in _LINKEDIN_JOB_URL_RE.finditer(text)
    ]


def parse_relative_posted_at(text: str) -> datetime | None:
    """Parse '2d', '5h', '1w' style recency markers from LinkedIn UI text."""
    m = re.search(r"\b(\d+)\s*(s|m|h|d|w|mo|yr)\b", text.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    now = datetime.now(UTC)
    if unit == "s":
        return now - timedelta(seconds=n)
    if unit == "m":
        return now - timedelta(minutes=n)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(days=n)
    if unit == "w":
        return now - timedelta(weeks=n)
    if unit == "mo":
        return now - timedelta(days=n * 30)
    if unit == "yr":
        return now - timedelta(days=n * 365)
    return None
