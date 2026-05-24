"""Naukri easy-apply auto-fill.

Naukri's apply button triggers a slide-in drawer where your uploaded resume is
pre-selected.  For most listings it's a 2-click flow: Apply → Submit.

Requires a logged-in Naukri session.  Run once:
    python scripts/setup_portal_login.py naukri
"""

from __future__ import annotations

import logging

from autoapply.channel.autofill.base import ATSAutoFiller
from autoapply.channel.autofill.portal_apply import (
    PortalApplyFailed,
    PortalLoginRequired,
    apply_on_portal,
)
from autoapply.config import get_qa_bank, get_settings
from autoapply.db.models import Application, AppStatus

log = logging.getLogger(__name__)


class NaukriAutoFiller(ATSAutoFiller):
    name = "naukri"

    async def apply(self, application_id: str) -> None:
        """Apply to a Naukri job. Updates Application.status to submitted or failed."""
        from autoapply.db.models import Application, AppStatus

        a = await Application.get(application_id)
        if a is None:
            raise ValueError(f"Application {application_id} not found")

        settings = get_settings()
        qa_bank = get_qa_bank()
        job_url = a.ats_url
        if not job_url:
            raise PortalApplyFailed("No ats_url on this application")

        try:
            result = await apply_on_portal(
                portal="naukri",
                job_url=job_url,
                profile_dir=settings.naukri_browser_dir,
                qa_bank=qa_bank,
                resume_pdf=settings.resume_pdf if settings.resume_pdf.exists() else None,
                headless=settings.headless_browser,
            )
            a.status = AppStatus.submitted
            a.error = None
            await a.save()
            log.info("naukri apply %s → %s", application_id, result)

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
