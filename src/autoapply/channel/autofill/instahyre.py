"""Instahyre easy-apply.

Requires a logged-in session - run once:
    python scripts/setup_portal_login.py instahyre
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


class InstahyreAutoFiller(ATSAutoFiller):
    name = "instahyre"

    async def apply(self, application_id: str) -> None:
        a = await Application.get(application_id)
        if a is None:
            raise ValueError(f"Application {application_id} not found")

        settings = get_settings()
        qa_bank = get_qa_bank()
        if not a.ats_url:
            raise PortalApplyFailed("No ats_url on this application")

        try:
            result = await apply_on_portal(
                portal="instahyre",
                job_url=a.ats_url,
                profile_dir=settings.instahyre_browser_dir,
                qa_bank=qa_bank,
                resume_pdf=settings.resume_pdf if settings.resume_pdf.exists() else None,
                headless=settings.headless_browser,
            )
            a.status = AppStatus.submitted
            a.error = None
            await a.save()
            log.info("instahyre apply %s -> %s", application_id, result)

        except (PortalLoginRequired, PortalApplyFailed) as exc:
            a.status = AppStatus.failed
            a.error = str(exc)
            await a.save()
            raise
