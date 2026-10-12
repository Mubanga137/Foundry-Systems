"""
FS-CAE Assisted Channel Adapter

Provides the fast-lane operator interface for policy-compliant assisted dispatch:
generates direct m.me launch URLs and prepared clipboard payloads, recording
truthful operator-confirmed sends.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.schemas.states import DispatchStatus
from app.services.outreach.adapters.base import AdapterResult, BaseChannelAdapter
from app.services.outreach.adapters.messenger_adapter import extract_fb_username

logger = logging.getLogger(__name__)


class AssistedAdapter(BaseChannelAdapter):
    """
    Assisted Mode Adapter for semi-automated, operator-confirmed dispatches.
    Prepares clipboard payload and direct browser action URLs.
    """

    def __init__(self, operator_confirmed: bool = True):
        self.operator_confirmed = operator_confirmed

    def prepare_payload(self, job: DispatchJob, business: Business) -> Dict[str, Any]:
        """
        Prepare clipboard content and deep-link launcher for operator execution.
        """
        fb_url = business.fb_page_url or job.recipient_target or ""
        username = extract_fb_username(fb_url)
        action_url = f"https://m.me/{username}" if username else "https://m.me/"

        return {
            "job_id": job.id,
            "business_id": business.id,
            "company_name": business.company_name,
            "action_url": action_url,
            "clipboard_content": job.rendered_content,
            "phone": business.phone_normalized or business.phone_raw,
            "channel": job.channel,
        }

    def send(
        self,
        job: DispatchJob,
        business: Business,
        operator_confirmed: Optional[bool] = None,
    ) -> AdapterResult:
        """
        Execute or stage assisted manual outreach.
        """
        if business.is_suppressed:
            return AdapterResult(
                success=False,
                status=DispatchStatus.BLOCKED,
                error_message="Recipient is suppressed/opted-out",
            )

        payload = self.prepare_payload(job, business)
        action_url = payload["action_url"]
        confirmed = self.operator_confirmed if operator_confirmed is None else operator_confirmed

        if not confirmed:
            logger.info("Assisted dispatch prepared for operator approval: %s", action_url)
            return AdapterResult(
                success=False,
                status=DispatchStatus.SENDING,
                error_message="Awaiting operator confirmation in assisted mode",
                requires_operator_action=True,
                action_url=action_url,
            )

        external_id = f"assisted_{uuid.uuid4().hex[:12]}"
        logger.info(
            "Assisted dispatch operator-confirmed for %s (id=%s)",
            business.company_name,
            external_id,
        )
        return AdapterResult(
            success=True,
            status=DispatchStatus.SENT,
            external_id=external_id,
            action_url=action_url,
            requires_operator_action=False,
        )
