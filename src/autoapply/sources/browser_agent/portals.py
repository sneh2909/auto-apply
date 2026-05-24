"""Portal configurations: search URL builders and job-link matchers.

Adding a new portal:
  1. Add a case in build_search_url()
  2. Add patterns in JOB_DETAIL_PATTERNS
  3. Create a scraper in sources/scrapers/<portal>.py

URL patterns are kept URL-path based (not CSS) — stable across redesigns.
"""

from __future__ import annotations

from urllib.parse import quote_plus

# URL patterns that identify a job DETAIL page (not a search results listing).
# Only href attribute patterns — not class names.
JOB_DETAIL_PATTERNS: dict[str, list[str]] = {
    "linkedin": [
        "linkedin.com/jobs/view/",
        "linkedin.com/feed/update/urn:li:activity:",  # hiring posts
    ],
    "naukri": [
        "naukri.com/job-listings-",
    ],
    "foundit": [
        "foundit.in/job/",
        "foundit.in/srp/job/",
    ],
    "instahyre": [
        "instahyre.com/candidates/opportunities/",
        "instahyre.com/job/",
    ],
}


def build_search_url(portal: str, role: str, location: str) -> str:
    """Return the direct search-results URL for a portal, role, and location."""
    r = quote_plus(role)
    loc = quote_plus(location)

    match portal:
        case "linkedin":
            # LinkedIn Jobs board — requires logged-in session for full results
            return (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={r}&location={loc}&f_TPR=r604800&sortBy=DD"
            )
        case "linkedin_posts":
            # LinkedIn content search — finds recruiter posts with HR emails
            return (
                f"https://www.linkedin.com/search/results/content/"
                f"?keywords={r}+hiring&datePosted=%22past-week%22"
            )
        case "naukri":
            loc_slug = location.lower().replace(" ", "-")
            return f"https://www.naukri.com/jobs-in-{loc_slug}?k={r}&experience=0"
        case "foundit":
            return f"https://www.foundit.in/srp/results?query={r}&location={loc}&sort=1"
        case "instahyre":
            return f"https://www.instahyre.com/search-jobs/?job_title={r}&location={loc}"
        case _:
            raise ValueError(f"unknown portal: {portal!r}")


def is_job_detail_url(portal: str, url: str) -> bool:
    """Return True if url looks like a single job detail page."""
    return any(pat in url for pat in JOB_DETAIL_PATTERNS.get(portal, []))


def filter_job_links(portal: str, links: list[dict]) -> list[dict]:
    """From a list of {text, href} dicts, return only likely job-detail links."""
    seen: set[str] = set()
    out: list[dict] = []
    for link in links:
        href = link.get("href", "")
        if href in seen:
            continue
        if is_job_detail_url(portal, href):
            seen.add(href)
            out.append(link)
    return out[:40]  # cap — LLM context budget
