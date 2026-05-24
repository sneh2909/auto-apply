"""LLM-guided portal apply automation.

Strategy: text-based Playwright (get_by_role / get_by_text) instead of CSS
class selectors, which break on site redesigns.  If the automated path fails
at any step, we surface a PortalLoginRequired or PortalApplyFailed exception
so the review UI can show the user a one-click "open in browser" fallback.

Supported portals: naukri, foundit, instahyre
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Patterns that indicate we've been redirected to a login wall
_LOGIN_URL_PATTERNS = [
    "login",
    "signin",
    "sign-in",
    "auth",
    "register",
]

# Ordered list of button texts to try when looking for the Apply button.
# These are exact regex anchors so we don't match "Continue with Google" etc.
_APPLY_TEXTS = [
    r"^Apply Now$",
    r"^Easy Apply$",
    r"^Quick Apply$",
    r"^1-Click Apply$",
    r"^Apply for this Job$",
    r"^Apply with Resume$",
    r"^Apply$",
]

# Text signals that mean we are NOT logged in.  If any of these appear
# prominently on a job page, the user hasn't authenticated to the portal.
_NOT_LOGGED_IN_SIGNALS = [
    "continue with google",
    "continue with facebook",
    "continue with linkedin",
    "login to apply",
    "register to apply",
    "sign up to apply",
    "create your free account",
    "create profile to apply",
    "join naukri",
    "join foundit",
]

# If any of these strings appear, we are logged in (overrides _NOT_LOGGED_IN_SIGNALS)
_LOGGED_IN_SIGNALS = [
    "logout",
    "log out",
    "sign out",
    "my naukri",
    "my profile",
]

# Texts that indicate we already applied to this job
_ALREADY_APPLIED_PATTERNS = [
    "already applied",
    "you have applied",
    "withdraw application",
    "application withdrawn",
]

# Texts that confirm a successful submission
_SUCCESS_SIGNALS = [
    "application submitted",
    "applied successfully",
    "successfully applied",
    "thank you for applying",
    "we have received your application",
    "your application has been",
    "withdraw application",  # post-apply UI changes Apply → Withdraw
]


class PortalLoginRequired(Exception):
    """Raised when the portal demands login before applying."""


class PortalApplyFailed(Exception):
    """Raised when the apply flow could not be completed."""


class PortalLoginFailed(Exception):
    """Raised when auto-login with email/password did not succeed."""


# Portal login URLs + label patterns for email / password fields
_PORTAL_LOGIN_SPECS: dict[str, dict] = {
    "naukri": {
        "url": "https://www.naukri.com/nlogin/login",
        "email_labels": ["Email ID / Username", "Email", "Email ID", "Username"],
        "password_labels": ["Password"],
        "submit_texts": ["Login", "Sign In", "Continue"],
        "success_url_fragment": "naukri.com",
        "failure_text_signals": ["invalid", "incorrect", "wrong password"],
    },
    "foundit": {
        "url": "https://www.foundit.in/login",
        "email_labels": ["Email", "Email ID", "Username"],
        "password_labels": ["Password"],
        "submit_texts": ["Sign In", "Login", "Continue"],
        "success_url_fragment": "foundit.in",
        "failure_text_signals": ["invalid", "incorrect"],
    },
    "instahyre": {
        "url": "https://www.instahyre.com/login",
        "email_labels": ["Email", "Email Address", "Username"],
        "password_labels": ["Password"],
        "submit_texts": ["Login", "Sign In"],
        "success_url_fragment": "instahyre.com",
        "failure_text_signals": ["invalid", "incorrect"],
    },
}


def _is_login_page(url: str) -> bool:
    url_lower = url.lower()
    return any(p in url_lower for p in _LOGIN_URL_PATTERNS)


async def _click_text(page: object, texts: list[str]) -> bool:
    """Try each text pattern in order; click the first match. Returns True on success."""
    for text in texts:
        try:
            loc = page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first  # type: ignore[attr-defined]
            if await loc.count() > 0:
                await loc.click()
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass
        # Fallback: link with that text
        try:
            loc = page.get_by_role("link", name=re.compile(text, re.IGNORECASE)).first  # type: ignore[attr-defined]
            if await loc.count() > 0:
                await loc.click()
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass
    return False


async def _fill_text_field(page: object, label_patterns: list[str], value: str) -> bool:
    """Try to find an input by its associated label text and fill it."""
    for pattern in label_patterns:
        try:
            field = page.get_by_label(re.compile(pattern, re.IGNORECASE)).first  # type: ignore[attr-defined]
            if await field.count() > 0:
                await field.fill(value)
                return True
        except Exception:
            pass
    return False


def _is_logged_in(page_text: str) -> bool:
    """Inspect visible text to infer login state. Logged-in signals win over not-logged-in signals."""
    t = page_text.lower()
    if any(s in t for s in _LOGGED_IN_SIGNALS):
        return True
    if any(s in t for s in _NOT_LOGGED_IN_SIGNALS):
        return False
    return True  # default optimistic — let the apply attempt decide


async def _try_auto_login(portal: str, profile_dir: Path) -> bool:
    """If credentials are saved for this portal, try to log in. Returns True on success."""
    try:
        from autoapply.config import get_portals_config
    except Exception:
        return False

    cfg = get_portals_config()
    creds = getattr(cfg, portal, None)
    if creds is None or not creds.is_set:
        return False

    log.info("[%s] not logged in — attempting auto-login with stored creds", portal)
    try:
        # Run a separate browser session for login so cookies persist to the same profile_dir
        await auto_login(
            portal=portal,
            email=creds.email,
            password=creds.password,
            profile_dir=profile_dir,
            headless=False,  # show window so user can solve captchas
        )
        return True
    except PortalLoginFailed as e:
        log.warning("[%s] auto-login failed: %s", portal, e)
        return False


async def apply_on_portal(
    portal: str,
    job_url: str,
    profile_dir: Path,
    qa_bank: dict[str, str],
    resume_pdf: Path | None,
    headless: bool = True,
) -> str:
    """Open a persistent browser session for the portal and attempt to apply.

    Flow:
      1. Navigate to job URL.
      2. Detect login state. If not logged in:
         a. Close this browser, run auto_login() if creds saved, otherwise raise.
         b. Re-open browser, re-navigate to job URL.
      3. Find an Apply button (exact text matches — never matches "Continue with Google").
      4. Click it; handle resume upload + screening questions if a modal appears.
      5. Submit.  Verify success via concrete confirmation text or "Withdraw" CTA.

    Returns 'submitted' or 'already_applied'.
    Raises PortalLoginRequired if login needed and auto-login wasn't possible.
    Raises PortalApplyFailed for any other failure with a useful message.
    """
    from autoapply.sources.browser_agent.browser import BrowserSession

    async def _open_and_check_login(attempt: int) -> tuple[bool, str]:
        """Open the browser, navigate, return (logged_in, page_text). Closes browser on return."""
        async with BrowserSession(headless=headless, profile_dir=profile_dir) as b:
            log.info("[%s] attempt %d: navigating to %s", portal, attempt, job_url)
            await b.navigate(job_url, wait="networkidle", delay=2.5)
            if _is_login_page(b.url):
                return False, ""
            text = await b.get_text(max_chars=8000)
            return _is_logged_in(text), text

    # ── Step 1+2: Make sure we're logged in ─────────────────────────────
    logged_in, _ = await _open_and_check_login(attempt=1)
    if not logged_in:
        log.info("[%s] login required", portal)
        if not await _try_auto_login(portal, profile_dir):
            raise PortalLoginRequired(
                f"{portal} session is not logged in. "
                f"Go to /credentials and either save email+password (then Auto-login) "
                f"or click Manual login."
            )
        # Verify the login took effect
        logged_in, _ = await _open_and_check_login(attempt=2)
        if not logged_in:
            raise PortalLoginRequired(
                f"{portal}: auto-login appeared to succeed but the job page still requires login. "
                f"Use Manual login on /credentials and complete it by hand."
            )

    # ── Step 3+: Apply ──────────────────────────────────────────────────
    async with BrowserSession(headless=headless, profile_dir=profile_dir) as b:
        page = b.page
        log.info("[%s] applying — navigating to %s", portal, job_url)
        await b.navigate(job_url, wait="networkidle", delay=2.5)
        page_text = (await b.get_text(max_chars=8000)).lower()

        if any(p in page_text for p in _ALREADY_APPLIED_PATTERNS):
            log.info("[%s] already applied to %s", portal, job_url)
            return "already_applied"

        # Click an Apply button (exact-anchored patterns).  Excludes "Continue with Google" etc.
        clicked = await _click_text(page, _APPLY_TEXTS)
        if not clicked:
            raise PortalApplyFailed(
                f"Couldn't find an Apply button on the job page. "
                f"Possible causes: not actually logged in, page hasn't finished loading, "
                f"or the button text on this listing is unusual."
            )

        await asyncio.sleep(2.5)

        # Did clicking Apply throw us to a login page?
        if _is_login_page(b.url):
            raise PortalLoginRequired(
                f"{portal} bounced to login after clicking Apply — session must have expired."
            )

        # Resume upload (only if file picker appeared)
        if resume_pdf and resume_pdf.exists():
            try:
                file_input = page.locator("input[type='file']").first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(resume_pdf))
                    await asyncio.sleep(1.5)
                    log.info("[%s] uploaded resume %s", portal, resume_pdf.name)
            except Exception as exc:
                log.warning("[%s] resume upload skipped: %s", portal, exc)

        # QA bank screening questions
        for question_key, answer in qa_bank.items():
            await _fill_text_field(page, [question_key], answer)

        # Click a Submit button (only fires if a confirmation modal appeared after the first click)
        await _click_text(
            page,
            ["Submit Application", "Submit", "Confirm Apply", "Done"],
        )

        # ── Verification ──────────────────────────────────────────────
        # Poll the page for up to ~10s for a definitive success signal.
        for _ in range(7):
            await asyncio.sleep(1.5)
            final_text = (await b.get_text(max_chars=3000)).lower()
            if any(s in final_text for s in _SUCCESS_SIGNALS):
                log.info("[%s] verified apply for %s", portal, job_url)
                return "submitted"
            if any(s in final_text for s in _ALREADY_APPLIED_PATTERNS):
                log.info("[%s] verified (already applied) for %s", portal, job_url)
                return "already_applied"

        raise PortalApplyFailed(
            "Apply was clicked but no confirmation appeared. "
            "Open the job link in your browser to check — the apply may have partially gone through."
        )


async def auto_login(
    portal: str,
    email: str,
    password: str,
    profile_dir: Path,
    headless: bool = False,
) -> str:
    """Open the portal's login page, fill email + password, submit, persist the session.

    Returns 'logged_in' on success.  Raises PortalLoginFailed otherwise.

    Defaults to headful so the user can solve captchas / 2FA challenges if they
    appear; we just wait for the page to leave the login URL.
    """
    from autoapply.sources.browser_agent.browser import BrowserSession

    spec = _PORTAL_LOGIN_SPECS.get(portal)
    if spec is None:
        raise PortalLoginFailed(f"Auto-login not supported for portal {portal!r}")

    if not email or not password:
        raise PortalLoginFailed(f"No credentials saved for {portal} — set them on /credentials first.")

    async with BrowserSession(headless=headless, profile_dir=profile_dir) as b:
        page = b.page
        log.info("[%s] auto-login: navigating to %s", portal, spec["url"])
        await b.navigate(spec["url"], wait="networkidle", delay=2.0)

        # Fill email
        email_filled = False
        for label in spec["email_labels"]:
            try:
                field = page.get_by_label(re.compile(label, re.IGNORECASE)).first  # type: ignore[attr-defined]
                if await field.count() > 0:
                    await field.fill(email)
                    email_filled = True
                    break
            except Exception:
                continue
        # Last resort: any input[type=email] or input[type=text] at top of page
        if not email_filled:
            try:
                field = page.locator("input[type='email']").first  # type: ignore[attr-defined]
                if await field.count() > 0:
                    await field.fill(email)
                    email_filled = True
            except Exception:
                pass

        if not email_filled:
            raise PortalLoginFailed(f"Could not find email field on {spec['url']}")

        # Fill password
        password_filled = False
        for label in spec["password_labels"]:
            try:
                field = page.get_by_label(re.compile(label, re.IGNORECASE)).first  # type: ignore[attr-defined]
                if await field.count() > 0:
                    await field.fill(password)
                    password_filled = True
                    break
            except Exception:
                continue
        if not password_filled:
            try:
                field = page.locator("input[type='password']").first  # type: ignore[attr-defined]
                if await field.count() > 0:
                    await field.fill(password)
                    password_filled = True
            except Exception:
                pass

        if not password_filled:
            raise PortalLoginFailed(f"Could not find password field on {spec['url']}")

        # Submit
        submitted = await _click_text(page, spec["submit_texts"])
        if not submitted:
            raise PortalLoginFailed("Could not find submit button on login form")

        # Wait for navigation away from login URL (or a captcha to be solved by user)
        await asyncio.sleep(3.0)
        for _ in range(20):  # up to ~30s
            current = b.url.lower()
            if "login" not in current and "signin" not in current:
                log.info("[%s] auto-login succeeded — landed on %s", portal, current)
                return "logged_in"
            text = (await b.get_text(max_chars=2000)).lower()
            if any(sig in text for sig in spec["failure_text_signals"]):
                raise PortalLoginFailed(f"{portal} rejected the credentials")
            await asyncio.sleep(1.5)

        raise PortalLoginFailed(f"Login didn't complete within timeout — captcha/2FA may need manual attention")
