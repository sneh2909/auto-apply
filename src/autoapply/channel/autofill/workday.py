"""Workday auto-fill.

Workday is unusual: each company has its own Workday tenant (visa.wd5.*, sap.wd3.*,
microsoft.wd5.*, etc.) and you create a separate account per tenant. So we can't
share a single login session — first apply per company always needs manual sign-in.

Strategy: open the apply URL in a per-tenant browser profile, click through the
"Apply with Resume" flow, upload the PDF, let Workday auto-fill from it, then
walk the page-1/page-2/page-3 buttons until we hit a Submit. Any captcha or
account-creation prompt halts the flow with PortalLoginRequired.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from autoapply.channel.autofill.base import ATSAutoFiller
from autoapply.channel.autofill.portal_apply import (
    PortalApplyFailed,
    PortalLoginRequired,
)
from autoapply.config import get_profile, get_qa_bank, get_settings
from autoapply.db.models import Application, AppStatus

log = logging.getLogger(__name__)


_LOGIN_SIGNALS = [
    "sign in",
    "create account",
    "create an account",
    "verify your email",
    "are you a returning candidate",
]


def _tenant_slug(url: str) -> str:
    """visa.wd5.myworkdayjobs.com → 'visa-wd5' (used as profile dir suffix)."""
    host = urlparse(url).netloc.lower()
    parts = host.split(".")
    # Take first two labels: e.g. visa + wd5
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return parts[0] if parts else "default"


class WorkdayAutoFiller(ATSAutoFiller):
    name = "workday"

    async def apply(self, application_id: str) -> None:
        from autoapply.sources.browser_agent.browser import BrowserSession

        a = await Application.get(application_id)
        if a is None:
            raise ValueError(f"Application {application_id} not found")

        settings = get_settings()
        profile = get_profile()
        qa_bank = get_qa_bank()
        job_url = a.ats_url
        if not job_url:
            raise PortalApplyFailed("No ats_url on this application")

        # Per-tenant profile dir under autoapply_home
        tenant = _tenant_slug(job_url)
        tenant_profile = settings.autoapply_home / f"workday-{tenant}"
        tenant_profile.mkdir(parents=True, exist_ok=True)

        try:
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=tenant_profile,
            ) as b:
                page = b.page
                log.info("[workday:%s] navigating to %s", tenant, job_url)
                await b.navigate(job_url, wait="networkidle", delay=3.0)

                # Check for login wall
                page_text = (await b.get_text(max_chars=4000)).lower()
                if any(sig in page_text for sig in _LOGIN_SIGNALS):
                    # Try clicking "Apply" / "Apply Manually" first — Workday's apply chooser
                    for txt in ("Apply", "Apply Now", "Apply Manually","Apply on Company site"):
                        try:
                            btn = page.get_by_role("button", name=re.compile(txt, re.IGNORECASE)).first
                            if await btn.count() > 0:
                                await btn.click()
                                await asyncio.sleep(2.0)
                                break
                        except Exception:
                            continue
                    page_text = (await b.get_text(max_chars=4000)).lower()
                    if any(sig in page_text for sig in _LOGIN_SIGNALS):
                        raise PortalLoginRequired(
                            f"Workday tenant {tenant} requires manual account sign-in. "
                            f"Open the URL in your browser and create an account, then retry."
                        )

                # Try the "Autofill with Resume" path first (some URLs land there directly)
                if "autofillwithresume" not in job_url.lower():
                    for txt in ("Autofill with Resume", "Apply with Resume", "Apply"):
                        try:
                            btn = page.get_by_role("button", name=re.compile(txt, re.IGNORECASE)).first
                            if await btn.count() > 0:
                                await btn.click()
                                await asyncio.sleep(2.5)
                                break
                        except Exception:
                            continue

                # Upload resume
                resume = settings.resume_pdf
                if resume.exists():
                    try:
                        file_input = page.locator("input[type='file']").first
                        if await file_input.count() > 0:
                            await file_input.set_input_files(str(resume))
                            await asyncio.sleep(4.0)  # Workday parses the resume — give it time
                            log.info("[workday:%s] uploaded resume", tenant)
                    except Exception as exc:
                        log.warning("[workday:%s] resume upload failed: %s", tenant, exc)

                # Fill identity fields (Workday usually pre-fills from resume but ensure key ones)
                fills = {
                    "First Name": profile.identity.full_name.split(" ")[0],
                    "Last Name":  profile.identity.full_name.split(" ")[-1],
                    "Email":      profile.identity.email_primary,
                    "Phone":      profile.identity.phone,
                }
                for label, value in fills.items():
                    if not value:
                        continue
                    try:
                        field = page.get_by_label(re.compile(label, re.IGNORECASE)).first
                        if await field.count() > 0:
                            current = (await field.input_value()) or ""
                            if not current.strip():
                                await field.fill(value)
                    except Exception:
                        pass

                # QA bank
                for question, answer in qa_bank.items():
                    try:
                        field = page.get_by_label(re.compile(re.escape(question), re.IGNORECASE)).first
                        if await field.count() > 0:
                            await field.fill(answer)
                    except Exception:
                        pass

                # Walk multi-page form: click Save and Continue / Next up to 6 times
                for step in range(6):
                    advanced = False
                    for txt in ("Save and Continue", "Next", "Continue"):
                        try:
                            btn = page.get_by_role("button", name=re.compile(txt, re.IGNORECASE)).first
                            if await btn.count() > 0:
                                await btn.click()
                                await asyncio.sleep(2.5)
                                advanced = True
                                break
                        except Exception:
                            continue
                    if not advanced:
                        break

                # Final submit
                submitted = False
                for txt in ("Submit", "Submit Application", "Apply"):
                    try:
                        btn = page.get_by_role("button", name=re.compile(txt, re.IGNORECASE)).first
                        if await btn.count() > 0:
                            await btn.click()
                            submitted = True
                            break
                    except Exception:
                        continue

                if not submitted:
                    raise PortalApplyFailed(
                        "Walked through Workday pages but couldn't find Submit — "
                        "likely a required field is missing. Open the URL and finish manually."
                    )

                await asyncio.sleep(3.0)
                final_text = (await b.get_text(max_chars=2000)).lower()
                success = any(
                    s in final_text
                    for s in ("thank you", "submitted", "we have received", "application has been")
                )
                a.status = AppStatus.submitted
                a.error = None
                await a.save()
                log.info("[workday:%s] applied to %s (success_signal=%s)", tenant, job_url, success)

        except PortalLoginRequired as exc:
            a.status = AppStatus.failed
            a.error = str(exc)
            await a.save()
            raise
        except PortalApplyFailed as exc:
            a.status = AppStatus.failed
            a.error = str(exc)
            await a.save()
            raise
        except Exception as exc:
            log.exception("[workday] apply unexpected error")
            a.status = AppStatus.failed
            a.error = f"unexpected: {exc}"
            await a.save()
            raise PortalApplyFailed(str(exc)) from exc
