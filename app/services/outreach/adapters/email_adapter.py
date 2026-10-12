"""
FS-CAE Email Channel Adapter

Handles outbound business email delivery with strict suppression enforcement,
syntax validation, and support for both mock/testing and SMTP delivery.
"""

from __future__ import annotations

import logging
import re
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.config import get_settings
from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.schemas.states import DispatchStatus
from app.services.outreach.adapters.base import AdapterResult, BaseChannelAdapter

logger = logging.getLogger(__name__)

# Basic RFC 5322 compliant email regex
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class EmailAdapter(BaseChannelAdapter):
    """Channel adapter for outbound email communication."""

    def __init__(
        self,
        test_mode: Optional[bool] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_from: Optional[str] = None,
    ):
        settings = get_settings()
        self.test_mode = test_mode if test_mode is not None else settings.EMAIL_TEST_MODE
        self.smtp_host = smtp_host or settings.SMTP_HOST
        self.smtp_port = smtp_port or settings.SMTP_PORT
        self.smtp_user = smtp_user or settings.SMTP_USER
        self.smtp_password = smtp_password or settings.SMTP_PASSWORD
        self.smtp_from = smtp_from or settings.SMTP_FROM or "procurement-review@foundry-systems.com"

    def validate_email(self, email: Optional[str]) -> bool:
        """Check email syntax validity."""
        if not email or not isinstance(email, str):
            return False
        return bool(EMAIL_REGEX.match(email.strip()))

    def send(self, job: DispatchJob, business: Business) -> AdapterResult:
        """
        Deliver message via email.

        Enforces:
          1. Suppression checks (business.is_suppressed -> BLOCKED).
          2. Email presence and syntax validation.
          3. Test mode simulation or actual SMTP transmission.
        """
        # 1. Suppression Check
        if business.is_suppressed:
            logger.warning(
                "Dispatch blocked for suppressed business %s (%s)",
                business.id,
                business.company_name,
            )
            return AdapterResult(
                success=False,
                status=DispatchStatus.BLOCKED,
                error_message="Recipient is suppressed/opted-out",
            )

        # 2. Email Presence & Syntax Validation
        target_email = (business.email or job.recipient_target or "").strip()
        if not self.validate_email(target_email):
            logger.error(
                "Invalid or missing recipient email for business %s: %s",
                business.id,
                target_email,
            )
            return AdapterResult(
                success=False,
                status=DispatchStatus.FAILED,
                error_message=f"Invalid or missing email address: '{target_email}'",
            )

        # 3. Test / Mock Mode
        if self.test_mode or not self.smtp_host:
            external_id = f"mock_email_{uuid.uuid4().hex[:12]}"
            logger.info(
                "[MOCK EMAIL SENT] To: %s | Subject: 3-Point Capability Review | External ID: %s",
                target_email,
                external_id,
            )
            return AdapterResult(
                success=True,
                status=DispatchStatus.SENT,
                external_id=external_id,
            )

        # 4. Live SMTP Mode
        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_from
            msg["To"] = target_email
            msg["Subject"] = f"Capability Review: {business.company_name}"
            msg.attach(MIMEText(job.rendered_content, "plain"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                if self.smtp_user and self.smtp_password:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            external_id = f"smtp_{uuid.uuid4().hex[:12]}"
            logger.info("Email delivered via SMTP to %s (id=%s)", target_email, external_id)
            return AdapterResult(
                success=True,
                status=DispatchStatus.SENT,
                external_id=external_id,
            )
        except Exception as exc:
            logger.exception("SMTP transmission failed for %s: %s", target_email, exc)
            return AdapterResult(
                success=False,
                status=DispatchStatus.FAILED,
                error_message=f"SMTP transmission error: {exc}",
            )
