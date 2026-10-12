"""
FS-CAE Batch Management Service

Manages operator staging, approval, and rejection workflows for outbound outreach.
Enforces channel validation and Layer 1 state transitions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import DispatchChannel, DispatchStatus, QualificationState
from app.services.outreach.message_compiler import MessageCompiler
from app.services.state_machine import transition_dispatch, transition_qualification

logger = logging.getLogger(__name__)


def determine_default_channel(business: Business) -> tuple[str, str]:
    """
    Select appropriate default channel and recipient target for a business.
    Order of preference:
      1. Facebook Messenger (if fb_page_url available)
      2. Business Email (if email available)
      3. Manual / Assisted Mode (fallback to phone or company name)
    """
    if business.fb_page_url and business.fb_page_url.strip():
        return DispatchChannel.FB_MESSENGER.value, business.fb_page_url.strip()
    elif business.email and business.email.strip():
        return DispatchChannel.EMAIL.value, business.email.strip()
    else:
        target = business.phone_normalized or business.company_name
        return DispatchChannel.MANUAL.value, target


def draft_outreach_for_qualified(
    db: Session,
    limit: int = 20,
    gemini_client: Optional[Any] = None,
) -> List[DispatchJob]:
    """
    Query QUALIFIED prospects, compile $100M Leads messages, create DRAFTED
    DispatchJob records, and transition qualifications to STAGED_FOR_OUTREACH.
    """
    compiler = MessageCompiler(gemini_client=gemini_client)

    # Find unsuppressed qualified prospects
    qualifications = (
        db.query(ProspectQualification)
        .join(Business, ProspectQualification.business_id == Business.id)
        .filter(
            ProspectQualification.qualification_state == QualificationState.QUALIFIED.value,
            Business.is_suppressed.is_(False),
        )
        .order_by(ProspectQualification.created_at.asc())
        .limit(limit)
        .all()
    )

    created_jobs: List[DispatchJob] = []
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    for qual in qualifications:
        business = qual.business
        channel, target = determine_default_channel(business)
        idempotency_key = f"{business.id}_{channel}_{date_str}"

        # Prevent duplicate drafted/queued job on same day
        existing_job = (
            db.query(DispatchJob)
            .filter(DispatchJob.idempotency_key == idempotency_key)
            .first()
        )
        if existing_job:
            logger.info("Job already exists with idempotency key %s", idempotency_key)
            continue

        # Compile message copy
        rendered_content = compiler.compile(business, qual)

        job = DispatchJob(
            business_id=business.id,
            channel=channel,
            status=DispatchStatus.DRAFTED.value,
            recipient_target=target,
            rendered_content=rendered_content,
            idempotency_key=idempotency_key,
            scheduled_for=now,
        )
        db.add(job)
        db.flush()

        # Transition qualification state to STAGED_FOR_OUTREACH
        transition_qualification(
            db,
            qual,
            QualificationState.STAGED_FOR_OUTREACH.value,
            actor="BATCH_MANAGER",
            reason=f"Drafted dispatch job {job.id} for {channel}",
        )

        created_jobs.append(job)

    db.commit()
    return created_jobs


def approve_batch(
    db: Session,
    job_ids: Optional[List[str]] = None,
    channel: Optional[str] = None,
) -> List[DispatchJob]:
    """
    Approve DRAFTED jobs and transition them to QUEUED.
    Optionally override or validate the channel.
    """
    if channel is not None:
        valid_channels = {c.value for c in DispatchChannel}
        if channel not in valid_channels:
            raise ValueError(
                f"Invalid target channel '{channel}'. Allowed channels: {sorted(valid_channels)}"
            )

    query = db.query(DispatchJob).filter(DispatchJob.status == DispatchStatus.DRAFTED.value)

    if job_ids is not None:
        query = query.filter(DispatchJob.id.in_(job_ids))

    jobs = query.all()
    approved_jobs: List[DispatchJob] = []

    for job in jobs:
        if channel is not None:
            job.channel = channel

        transition_dispatch(
            db,
            job,
            DispatchStatus.QUEUED.value,
            actor="OPERATOR",
            reason=f"Batch operator approved for dispatch via {job.channel}",
        )
        approved_jobs.append(job)

    db.commit()
    return approved_jobs


def reject_batch(
    db: Session,
    job_ids: List[str],
    reason: str,
) -> List[DispatchJob]:
    """
    Reject jobs and mark them as BLOCKED with a mandatory reason.
    """
    if not reason or not reason.strip():
        raise ValueError("Rejection reason is required")

    jobs = (
        db.query(DispatchJob)
        .filter(
            DispatchJob.id.in_(job_ids),
            DispatchJob.status.in_([DispatchStatus.DRAFTED.value, DispatchStatus.QUEUED.value]),
        )
        .all()
    )

    rejected_jobs: List[DispatchJob] = []
    for job in jobs:
        transition_dispatch(
            db,
            job,
            DispatchStatus.BLOCKED.value,
            blocked_reason=reason.strip(),
            actor="OPERATOR",
            reason=f"Batch rejected: {reason.strip()}",
        )
        rejected_jobs.append(job)

    db.commit()
    return rejected_jobs
