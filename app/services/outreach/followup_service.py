"""
FS-CAE Context-Aware Follow-Up Service

Evaluates stalled conversations across pipeline stages:
  - AWAITING_REPLY: 72 hours
  - AUDIT_DISPATCHED: 48 hours
  - PROPOSAL_REQUESTED: 72 hours
  - INVOICED: 48 hours

Enforces maximum 2 attempts per stage and automatically cancels pending follow-ups
upon reply, payment, stage advancement, or opt-out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.business import Business
from app.models.commercial import Invoice
from app.models.conversation import Conversation
from app.models.dispatch import DispatchJob
from app.schemas.states import ConversationState, DispatchStatus
from app.services.state_machine import transition_dispatch

logger = logging.getLogger(__name__)

STAGE_THRESHOLDS_HOURS = {
    ConversationState.AWAITING_REPLY.value: 72,
    ConversationState.AUDIT_DISPATCHED.value: 48,
    ConversationState.PROPOSAL_REQUESTED.value: 72,
    ConversationState.INVOICED.value: 48,
}

MAX_FOLLOWUPS_PER_STAGE = 2


def generate_followup_copy(conv: Conversation, business: Business, db: Session) -> str:
    """Generate concise, professional follow-up copy tailored to current conversation state."""
    name = business.company_name
    loc = business.location_district or "operational routes"

    if conv.state == ConversationState.AWAITING_REPLY.value:
        return (
            f"Checking in briefly on our note regarding operations along {loc} for {name} — "
            f"did you have a chance to review the capability checklist?"
        )
    elif conv.state == ConversationState.AUDIT_DISPATCHED.value:
        return (
            f"Touching base on the 3-point capability review we shared for {name} — "
            f"happy to answer any technical questions on mobile RFQ integration when convenient."
        )
    elif conv.state == ConversationState.PROPOSAL_REQUESTED.value:
        return (
            f"Following up on the Capability Portal proposal for {name} — "
            f"please let us know if your executive team has any questions on the milestone schedule."
        )
    elif conv.state == ConversationState.INVOICED.value:
        invoice = (
            db.query(Invoice)
            .join(Invoice.proposal)
            .filter(Invoice.proposal.has(business_id=business.id))
            .order_by(Invoice.created_at.desc())
            .first()
        )
        inv_ref = invoice.invoice_number if invoice else "mobilization invoice"
        return (
            f"Following up on {inv_ref} for {name} — "
            f"please let us know once the transfer reference is confirmed so we can schedule engineering setup."
        )
    return f"Checking in regarding our discussions for {name}."


def cancel_pending_followups(
    db: Session,
    business_id: str,
    reason: str = "State changed or prospect responded",
) -> int:
    """
    Cancel and mark BLOCKED any pending follow-up dispatch jobs for a business.
    Returns count of canceled jobs.
    """
    jobs = (
        db.query(DispatchJob)
        .filter(
            DispatchJob.business_id == business_id,
            DispatchJob.idempotency_key.like("followup_%"),
            DispatchJob.status.in_([
                DispatchStatus.DRAFTED.value,
                DispatchStatus.QUEUED.value,
                DispatchStatus.SENDING.value,
            ]),
        )
        .all()
    )

    canceled_count = 0
    for job in jobs:
        transition_dispatch(
            db,
            job,
            DispatchStatus.BLOCKED.value,
            blocked_reason=reason,
            actor="FOLLOWUP_SERVICE",
            reason=reason,
        )
        canceled_count += 1

    if canceled_count > 0:
        logger.info(
            "Canceled %d pending follow-up jobs for business %s: %s",
            canceled_count,
            business_id,
            reason,
        )
        db.commit()

    return canceled_count


def evaluate_stalled_conversations_and_queue_followups(
    db: Session,
    now: Optional[datetime] = None,
) -> List[DispatchJob]:
    """
    Evaluate all active conversations. If stalled beyond threshold and under attempt cap,
    queue a context-aware follow-up dispatch job.
    """
    current_time = now or datetime.now(timezone.utc)
    queued_jobs: List[DispatchJob] = []

    conversations = (
        db.query(Conversation)
        .join(Business, Conversation.business_id == Business.id)
        .filter(
            Conversation.state.in_(list(STAGE_THRESHOLDS_HOURS.keys())),
            Business.is_suppressed.is_(False),
        )
        .all()
    )

    for conv in conversations:
        business = conv.business
        threshold_hours = STAGE_THRESHOLDS_HOURS.get(conv.state, 72)
        activity_at = conv.last_activity_at
        if activity_at.tzinfo is None:
            activity_at = activity_at.replace(tzinfo=timezone.utc)
        now_tz = current_time
        if now_tz.tzinfo is None:
            now_tz = now_tz.replace(tzinfo=timezone.utc)
        elapsed_hours = (now_tz - activity_at).total_seconds() / 3600.0

        if elapsed_hours < threshold_hours:
            continue

        # Count previous follow-ups for this stage
        prefix = f"followup_{business.id}_{conv.state}_%"
        previous_count = (
            db.query(DispatchJob)
            .filter(
                DispatchJob.business_id == business.id,
                DispatchJob.idempotency_key.like(prefix),
            )
            .count()
        )

        if previous_count >= MAX_FOLLOWUPS_PER_STAGE:
            logger.debug(
                "Max follow-up attempts (%d) reached for business %s in state %s",
                MAX_FOLLOWUPS_PER_STAGE,
                business.id,
                conv.state,
            )
            continue

        attempt_number = previous_count + 1
        idempotency_key = f"followup_{business.id}_{conv.state}_{attempt_number}"

        # Ensure not already existing
        existing = (
            db.query(DispatchJob)
            .filter(DispatchJob.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            continue

        content = generate_followup_copy(conv, business, db)
        target = business.fb_page_url or business.email or business.phone_normalized or business.company_name

        job = DispatchJob(
            business_id=business.id,
            channel=conv.channel,
            status=DispatchStatus.QUEUED.value,
            recipient_target=target,
            rendered_content=content,
            idempotency_key=idempotency_key,
            scheduled_for=current_time,
        )
        db.add(job)
        queued_jobs.append(job)
        logger.info(
            "Queued follow-up job %s (attempt %d) for %s in %s",
            idempotency_key,
            attempt_number,
            business.company_name,
            conv.state,
        )

    db.commit()
    return queued_jobs


class FollowupService:
    """Class wrapper for follow-up operations."""

    def __init__(self, db: Session):
        self.db = db

    def evaluate_and_queue(self, now: Optional[datetime] = None) -> List[DispatchJob]:
        return evaluate_stalled_conversations_and_queue_followups(self.db, now=now)

    def cancel_followups(self, business_id: str, reason: str = "State changed") -> int:
        return cancel_pending_followups(self.db, business_id, reason=reason)
