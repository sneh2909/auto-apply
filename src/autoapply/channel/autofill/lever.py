"""Lever auto-fill.

Lever's standard application form (jobs.lever.co/<company>/<job-uuid>/apply)
uses labels like "Full name", "Email", "Phone", "Resume/CV", "Current company",
"LinkedIn URL".  Same text-based Playwright approach as Greenhouse.
"""

from __future__ import annotations

import asyncio
import logging
import re

from autoapply.channel.autofill.base import ATSAutoFiller
from autoapply.channel.autofill.portal_apply import PortalApplyFailed
from autoapply.config import get_profile, get_qa_bank, get_settings
from autoapply.db.models import Application, AppStatus

log = logging.getLogger(__name__)


class LeverAutoFiller(ATSAutoFiller):
    name = "lever"

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

        # Lever apply form is at /apply suffix
        apply_url = job_url if job_url.rstrip("/").endswith("/apply") else job_url.rstrip("/") + "/apply"

        try:
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=settings.browser_profile_dir,
            ) as b:
                page = b.page
                log.info("[lever] navigating to %s", apply_url)
                await b.navigate(apply_url, wait="networkidle", delay=2.0)

                fills = {
                    "Full name": profile.identity.full_name,
                    "Name": profile.identity.full_name,
                    "Email": profile.identity.email_primary,
                    "Phone": profile.identity.phone,
                    "Current company": profile.career.current_company,
                    "Current location": profile.identity.location,
                    "LinkedIn": profile.identity.linkedin,
                    "GitHub": profile.identity.github,
                    "Portfolio": profile.identity.portfolio,
                    "Website": profile.identity.portfolio,
                }
                for label, value in fills.items():
                    if not value:
                        continue
                    try:
                        field = page.get_by_label(re.compile(label, re.IGNORECASE)).first
                        if await field.count() > 0:
                            await field.fill(value)
                    except Exception as exc:
                        log.debug("[lever] couldn't fill %r: %s", label, exc)

                # Resume
                if settings.resume_pdf.exists():
                    try:
                        file_input = page.locator("input[type='file']").first
                        if await file_input.count() > 0:
                            await file_input.set_input_files(str(settings.resume_pdf))
                            await asyncio.sleep(1.5)
                    except Exception as exc:
                        log.warning("[lever] resume upload failed: %s", exc)

                # QA bank
                for question, answer in qa_bank.items():
                    try:
                        field = page.get_by_label(re.compile(re.escape(question), re.IGNORECASE)).first
                        if await field.count() > 0:
                            await field.fill(answer)
                    except Exception:
                        pass

                submitted = False
                for btn_text in ("Submit application", "Submit", "Apply"):
                    try:
                        btn = page.get_by_role("button", name=re.compile(btn_text, re.IGNORECASE)).first
                        if await btn.count() > 0:
                            await btn.click()
                            submitted = True
                            break
                    except Exception:
                        continue

                if not submitted:
                    raise PortalApplyFailed("Could not find Submit button on Lever form")

                await asyncio.sleep(3.0)
                final_text = (await b.get_text(max_chars=2000)).lower()
                success = any(
                    s in final_text
                    for s in ("submitted", "thank you", "we received", "successfully")
                )
                a.status = AppStatus.submitted
                a.error = None
                await a.save()
                log.info("[lever] applied to %s (success_signal=%s)", apply_url, success)

        except PortalApplyFailed as exc:
            a.status = AppStatus.failed
            a.error = str(exc)
            await a.save()
            raise
        except Exception as exc:
            log.exception("[lever] apply failed for %s", application_id)
            a.status = AppStatus.failed
            a.error = f"unexpected: {exc}"
            await a.save()
            raise PortalApplyFailed(str(exc)) from exc
