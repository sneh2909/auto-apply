"""Greenhouse auto-fill.

Greenhouse's standard application form is the same across all companies
(boards.greenhouse.io/<company>/jobs/<id>). We use Playwright's get_by_label()
which matches against the rendered <label> text - more robust than CSS class
selectors which Greenhouse occasionally tweaks.

Field map (standard Greenhouse):
    First Name, Last Name, Email, Phone, Resume/CV, LinkedIn URL, Website
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


class GreenhouseAutoFiller(ATSAutoFiller):
    name = "greenhouse"

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

        try:
            async with BrowserSession(
                headless=settings.headless_browser,
                profile_dir=settings.browser_profile_dir,
            ) as b:
                page = b.page
                log.info("[greenhouse] navigating to %s", job_url)
                await b.navigate(job_url, wait="networkidle", delay=2.0)

                # Click "Apply" if the form isn't already visible
                try:
                    apply_btn = page.get_by_role("link", name=re.compile("apply", re.IGNORECASE)).first
                    if await apply_btn.count() > 0:
                        await apply_btn.click()
                        await asyncio.sleep(2.0)
                except Exception:
                    pass

                # Fill identity fields by label
                first_name, _, last_name = profile.identity.full_name.partition(" ")
                if not last_name:
                    last_name = first_name

                fills = {
                    "First Name": first_name,
                    "Last Name": last_name,
                    "Email": profile.identity.email_primary,
                    "Phone": profile.identity.phone,
                    "LinkedIn": profile.identity.linkedin,
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
                        log.debug("[greenhouse] couldn't fill %r: %s", label, exc)

                # Upload resume
                if settings.resume_pdf.exists():
                    try:
                        file_input = page.locator("input[type='file']").first
                        if await file_input.count() > 0:
                            await file_input.set_input_files(str(settings.resume_pdf))
                            await asyncio.sleep(1.5)
                    except Exception as exc:
                        log.warning("[greenhouse] resume upload failed: %s", exc)

                # QA bank fills (best-effort)
                for question, answer in qa_bank.items():
                    try:
                        field = page.get_by_label(re.compile(re.escape(question), re.IGNORECASE)).first
                        if await field.count() > 0:
                            await field.fill(answer)
                    except Exception:
                        pass

                # Submit
                submitted = False
                for btn_text in ("Submit Application", "Submit", "Apply"):
                    try:
                        btn = page.get_by_role("button", name=re.compile(btn_text, re.IGNORECASE)).first
                        if await btn.count() > 0:
                            await btn.click()
                            submitted = True
                            break
                    except Exception:
                        continue

                if not submitted:
                    raise PortalApplyFailed("Could not find Submit button on Greenhouse form")

                await asyncio.sleep(3.0)
                final_text = (await b.get_text(max_chars=2000)).lower()
                success = any(
                    s in final_text
                    for s in ("application submitted", "thank you", "we received", "successfully")
                )
                a.status = AppStatus.submitted
                a.error = None
                await a.save()
                log.info("[greenhouse] applied to %s (success_signal=%s)", job_url, success)

        except PortalApplyFailed as exc:
            a.status = AppStatus.failed
            a.error = str(exc)
            await a.save()
            raise
        except Exception as exc:
            log.exception("[greenhouse] apply failed for %s", application_id)
            a.status = AppStatus.failed
            a.error = f"unexpected: {exc}"
            await a.save()
            raise PortalApplyFailed(str(exc)) from exc
