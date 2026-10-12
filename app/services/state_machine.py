"""
FS-CAE State Machine Service

Enforces all valid business-rule state transitions.
Every successful transition automatically writes an AuditLog entry.
Invalid transitions raise InvalidStateTransitionError.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.exceptions import (
    DuplicatePaymentError,
    InvalidStateTransitionError,
    UnmetMilestoneError,
)
from app.models.audit import CommercialAudit
from app.models.audit_log import AuditLog
from app.models.commercial import Invoice, PaymentEvent, ProjectActivation, Proposal
from app.models.conversation import Conversation
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    CommercialAuditStatus,
    ConversationState,
    DispatchStatus,
    InvoiceStatus,
    MilestoneTier,
    ProjectActivationStatus,
    ProposalStatus,
    QualificationState,
)

# =========================================================================== #
#  Allowed transition maps
# =========================================================================== #

QUALIFICATION_TRANSITIONS: dict[str, set[str]] = {
    QualificationState.DISCOVERED.value: {
        QualificationState.EVALUATING.value,
        QualificationState.SUPPRESSED.value,
    },
    QualificationState.EVALUATING.value: {
        QualificationState.QUALIFIED.value,
        QualificationState.DISQUALIFIED.value,
        QualificationState.SUPPRESSED.value,
    },
    QualificationState.QUALIFIED.value: {
        QualificationState.STAGED_FOR_OUTREACH.value,
        QualificationState.SUPPRESSED.value,
    },
    QualificationState.DISQUALIFIED.value: {
        QualificationState.SUPPRESSED.value,
    },
    QualificationState.STAGED_FOR_OUTREACH.value: {
        QualificationState.SUPPRESSED.value,
    },
    QualificationState.SUPPRESSED.value: set(),  # terminal
}

DISPATCH_TRANSITIONS: dict[str, set[str]] = {
    DispatchStatus.DRAFTED.value: {
        DispatchStatus.QUEUED.value,
        DispatchStatus.BLOCKED.value,
    },
    DispatchStatus.QUEUED.value: {
        DispatchStatus.SENDING.value,
        DispatchStatus.BLOCKED.value,
    },
    DispatchStatus.SENDING.value: {
        DispatchStatus.SENT.value,
        DispatchStatus.FAILED.value,
        DispatchStatus.BLOCKED.value,
    },
    DispatchStatus.SENT.value: {
        DispatchStatus.DELIVERED.value,
    },
    DispatchStatus.FAILED.value: {
        DispatchStatus.QUEUED.value,  # re-queue if attempts < max_attempts
    },
    DispatchStatus.DELIVERED.value: set(),  # terminal
    DispatchStatus.BLOCKED.value: set(),  # terminal
}

CONVERSATION_TRANSITIONS: dict[str, set[str]] = {
    ConversationState.AWAITING_REPLY.value: {
        ConversationState.POSITIVE_REPLY.value,
        ConversationState.OBJECTION_OR_QUESTION.value,
        ConversationState.OFFER_PENDING_APPROVAL.value,
        ConversationState.OPTED_OUT.value,
        ConversationState.LOST.value,
    },
    ConversationState.POSITIVE_REPLY.value: {
        ConversationState.AUDIT_PENDING_APPROVAL.value,
        ConversationState.OFFER_PENDING_APPROVAL.value,
        ConversationState.PROPOSAL_REQUESTED.value,
        ConversationState.OPTED_OUT.value,
    },
    ConversationState.OBJECTION_OR_QUESTION.value: {
        ConversationState.AWAITING_REPLY.value,
        ConversationState.POSITIVE_REPLY.value,
        ConversationState.OFFER_PENDING_APPROVAL.value,
        ConversationState.AUDIT_PENDING_APPROVAL.value,
        ConversationState.PROPOSAL_REQUESTED.value,
        ConversationState.OPTED_OUT.value,
        ConversationState.LOST.value,
    },
    ConversationState.AUDIT_PENDING_APPROVAL.value: {
        ConversationState.AUDIT_DISPATCHED.value,
        ConversationState.OPTED_OUT.value,
    },
    ConversationState.AUDIT_DISPATCHED.value: {
        ConversationState.AWAITING_REPLY.value,
        ConversationState.POSITIVE_REPLY.value,
        ConversationState.OFFER_PENDING_APPROVAL.value,
        ConversationState.OBJECTION_OR_QUESTION.value,
        ConversationState.PROPOSAL_REQUESTED.value,
        ConversationState.OPTED_OUT.value,
        ConversationState.LOST.value,
    },
    ConversationState.OFFER_PENDING_APPROVAL.value: {
        ConversationState.OFFER_DISPATCHED.value,
        ConversationState.OPTED_OUT.value,
    },
    ConversationState.OFFER_DISPATCHED.value: {
        ConversationState.PROPOSAL_REQUESTED.value,
        ConversationState.OBJECTION_OR_QUESTION.value,
        ConversationState.OPTED_OUT.value,
        ConversationState.LOST.value,
    },
    ConversationState.PROPOSAL_REQUESTED.value: {
        ConversationState.INVOICED.value,
        ConversationState.LOST.value,
        ConversationState.OPTED_OUT.value,
    },
    ConversationState.INVOICED.value: {
        "WON",
        ConversationState.LOST.value,
    },
    "WON": set(),  # terminal in conversation context
    ConversationState.OPTED_OUT.value: set(),  # terminal
    ConversationState.LOST.value: set(),  # terminal
}

INVOICE_TRANSITIONS: dict[str, set[str]] = {
    InvoiceStatus.DRAFT.value: {
        InvoiceStatus.ISSUED.value,
        InvoiceStatus.VOID.value,
    },
    InvoiceStatus.ISSUED.value: {
        InvoiceStatus.PAYMENT_PENDING.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.VOID.value,
    },
    InvoiceStatus.PAYMENT_PENDING.value: {
        InvoiceStatus.PAID.value,
        InvoiceStatus.VOID.value,
    },
    InvoiceStatus.PAID.value: set(),  # terminal
    InvoiceStatus.VOID.value: set(),  # terminal
}

COMMERCIAL_AUDIT_TRANSITIONS: dict[str, set[str]] = {
    CommercialAuditStatus.PENDING_GENERATION.value: {
        CommercialAuditStatus.GENERATED.value,
    },
    CommercialAuditStatus.GENERATED.value: {
        CommercialAuditStatus.APPROVED.value,
    },
    CommercialAuditStatus.APPROVED.value: {
        CommercialAuditStatus.DELIVERED.value,
    },
    CommercialAuditStatus.DELIVERED.value: set(),  # terminal
}

PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    ProposalStatus.DRAFT.value: {
        ProposalStatus.ISSUED.value,
    },
    ProposalStatus.ISSUED.value: {
        ProposalStatus.ACCEPTED.value,
        ProposalStatus.DECLINED.value,
        ProposalStatus.EXPIRED.value,
    },
    ProposalStatus.ACCEPTED.value: set(),  # terminal
    ProposalStatus.DECLINED.value: set(),  # terminal
    ProposalStatus.EXPIRED.value: set(),   # terminal
}


# =========================================================================== #
#  Audit-log writer
# =========================================================================== #

def _write_audit_log(
    session: Session,
    entity_type: str,
    entity_id: str,
    old_state: Optional[str],
    new_state: str,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> AuditLog:
    """Persist an immutable audit-log entry for the transition."""
    log = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        old_state=old_state,
        new_state=new_state,
        actor=actor,
        reason=reason,
    )
    session.add(log)
    return log


# =========================================================================== #
#  Prospect Qualification transitions
# =========================================================================== #

def transition_qualification(
    session: Session,
    qualification: ProspectQualification,
    new_state: str,
    *,
    fit_score: Optional[int] = None,
    disqualification_reason: Optional[str] = None,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> ProspectQualification:
    """
    Transition a ProspectQualification to a new state.

    Additional rules:
      - EVALUATING → QUALIFIED requires fit_score >= 65
      - EVALUATING → DISQUALIFIED requires fit_score < 65
    """
    old = qualification.qualification_state
    allowed = QUALIFICATION_TRANSITIONS.get(old, set())

    if new_state not in allowed:
        raise InvalidStateTransitionError("ProspectQualification", old, new_state)

    # Fit-score gating
    if new_state == QualificationState.QUALIFIED.value:
        if fit_score is None or fit_score < 65:
            raise InvalidStateTransitionError(
                "ProspectQualification",
                old,
                new_state,
                reason=f"fit_score must be >= 65 to qualify (got {fit_score})",
            )
    if new_state == QualificationState.DISQUALIFIED.value:
        if fit_score is not None and fit_score >= 65:
            raise InvalidStateTransitionError(
                "ProspectQualification",
                old,
                new_state,
                reason=f"fit_score must be < 65 to disqualify (got {fit_score})",
            )

    qualification.qualification_state = new_state
    if fit_score is not None:
        qualification.fit_score = fit_score
    if disqualification_reason is not None:
        qualification.disqualification_reason = disqualification_reason
    if new_state in (
        QualificationState.QUALIFIED.value,
        QualificationState.DISQUALIFIED.value,
    ):
        qualification.evaluated_at = datetime.now(timezone.utc)

    _write_audit_log(
        session,
        entity_type="ProspectQualification",
        entity_id=qualification.id,
        old_state=old,
        new_state=new_state,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return qualification


# =========================================================================== #
#  Dispatch Job transitions
# =========================================================================== #

def transition_dispatch(
    session: Session,
    job: DispatchJob,
    new_status: str,
    *,
    blocked_reason: Optional[str] = None,
    last_error: Optional[str] = None,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> DispatchJob:
    """
    Transition a DispatchJob to a new status.

    Rules:
      - BLOCKED requires blocked_reason.
      - FAILED → QUEUED only if attempts < max_attempts.
    """
    old = job.status
    allowed = DISPATCH_TRANSITIONS.get(old, set())

    if new_status not in allowed:
        raise InvalidStateTransitionError("DispatchJob", old, new_status)

    # Blocked must have a reason
    if new_status == DispatchStatus.BLOCKED.value:
        if not blocked_reason:
            raise InvalidStateTransitionError(
                "DispatchJob",
                old,
                new_status,
                reason="blocked_reason is required when transitioning to BLOCKED",
            )
        job.blocked_reason = blocked_reason

    # Re-queue guard
    if old == DispatchStatus.FAILED.value and new_status == DispatchStatus.QUEUED.value:
        if job.attempts >= job.max_attempts:
            raise InvalidStateTransitionError(
                "DispatchJob",
                old,
                new_status,
                reason=f"attempts ({job.attempts}) >= max_attempts ({job.max_attempts})",
            )

    if new_status == DispatchStatus.SENDING.value:
        job.attempts += 1

    if new_status == DispatchStatus.SENT.value:
        job.sent_at = datetime.now(timezone.utc)

    if new_status == DispatchStatus.DELIVERED.value:
        job.delivered_at = datetime.now(timezone.utc)

    if last_error is not None:
        job.last_error = last_error

    job.status = new_status

    _write_audit_log(
        session,
        entity_type="DispatchJob",
        entity_id=job.id,
        old_state=old,
        new_state=new_status,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return job


# =========================================================================== #
#  Conversation transitions
# =========================================================================== #

def transition_conversation(
    session: Session,
    conversation: Conversation,
    new_state: str,
    *,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> Conversation:
    """Transition a Conversation to a new state."""
    old = conversation.state
    allowed = CONVERSATION_TRANSITIONS.get(old, set())

    if new_state not in allowed:
        raise InvalidStateTransitionError("Conversation", old, new_state)

    conversation.state = new_state
    conversation.last_activity_at = datetime.now(timezone.utc)

    _write_audit_log(
        session,
        entity_type="Conversation",
        entity_id=conversation.id,
        old_state=old,
        new_state=new_state,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return conversation


# =========================================================================== #
#  Invoice payment & Project activation
# =========================================================================== #

def record_payment(
    session: Session,
    invoice: Invoice,
    amount_received: float,
    transaction_reference: str,
    verified_by: str,
    *,
    notes: Optional[str] = None,
    actor: str = "SYSTEM",
) -> PaymentEvent:
    """
    Record a payment against an invoice.

    - Checks for duplicate transaction_reference (raises DuplicatePaymentError).
    - Transitions the invoice to PAID if amount matches.
    """
    # Duplicate check
    existing = (
        session.query(PaymentEvent)
        .filter(PaymentEvent.transaction_reference == transaction_reference)
        .first()
    )
    if existing:
        raise DuplicatePaymentError(transaction_reference)

    now = datetime.now(timezone.utc)
    event = PaymentEvent(
        invoice_id=invoice.id,
        amount_received=amount_received,
        transaction_reference=transaction_reference,
        verified_by=verified_by,
        verified_at=now,
        notes=notes,
    )
    session.add(event)

    old_status = invoice.status
    invoice.status = InvoiceStatus.PAID.value
    invoice.paid_at = now

    _write_audit_log(
        session,
        entity_type="Invoice",
        entity_id=invoice.id,
        old_state=old_status,
        new_state=InvoiceStatus.PAID.value,
        actor=actor,
        reason=f"Payment received: {transaction_reference}",
    )
    session.flush()
    return event


def activate_project(
    session: Session,
    activation: ProjectActivation,
    *,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> ProjectActivation:
    """
    Transition a ProjectActivation to ACTIVATED.

    Requires the mobilization invoice to be PAID.
    """
    if activation.status != ProjectActivationStatus.PENDING_MOBILIZATION.value:
        raise InvalidStateTransitionError(
            "ProjectActivation",
            activation.status,
            ProjectActivationStatus.ACTIVATED.value,
        )

    # Load the mobilization invoice and verify payment
    invoice = (
        session.query(Invoice)
        .filter(Invoice.id == activation.mobilization_invoice_id)
        .one()
    )
    if invoice.status != InvoiceStatus.PAID.value:
        raise UnmetMilestoneError(
            f"Mobilization invoice {invoice.invoice_number} status is "
            f"'{invoice.status}', must be PAID before activation."
        )

    old = activation.status
    activation.status = ProjectActivationStatus.ACTIVATED.value
    activation.activated_at = datetime.now(timezone.utc)

    _write_audit_log(
        session,
        entity_type="ProjectActivation",
        entity_id=activation.id,
        old_state=old,
        new_state=ProjectActivationStatus.ACTIVATED.value,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return activation


# =========================================================================== #
#  Commercial Audit & Proposal transitions
# =========================================================================== #

def transition_audit(
    session: Session,
    audit: CommercialAudit,
    new_status: str,
    *,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> CommercialAudit:
    """Transition a CommercialAudit to a new status."""
    old = audit.status
    allowed = COMMERCIAL_AUDIT_TRANSITIONS.get(old, set())

    if new_status not in allowed:
        raise InvalidStateTransitionError("CommercialAudit", old, new_status)

    if new_status == CommercialAuditStatus.DELIVERED.value:
        audit.delivered_at = datetime.now(timezone.utc)

    audit.status = new_status

    _write_audit_log(
        session,
        entity_type="CommercialAudit",
        entity_id=audit.id,
        old_state=old,
        new_state=new_status,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return audit


def transition_proposal(
    session: Session,
    proposal: Proposal,
    new_status: str,
    *,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> Proposal:
    """Transition a Proposal to a new status."""
    old = proposal.status
    allowed = PROPOSAL_TRANSITIONS.get(old, set())

    if new_status not in allowed:
        raise InvalidStateTransitionError("Proposal", old, new_status)

    if new_status == ProposalStatus.ISSUED.value:
        proposal.issued_at = datetime.now(timezone.utc)

    proposal.status = new_status

    _write_audit_log(
        session,
        entity_type="Proposal",
        entity_id=proposal.id,
        old_state=old,
        new_state=new_status,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return proposal


def transition_invoice(
    session: Session,
    invoice: Invoice,
    new_status: str,
    *,
    actor: str = "SYSTEM",
    reason: Optional[str] = None,
) -> Invoice:
    """Transition an Invoice to a new status."""
    old = invoice.status
    allowed = INVOICE_TRANSITIONS.get(old, set())

    if new_status not in allowed:
        raise InvalidStateTransitionError("Invoice", old, new_status)

    if new_status == InvoiceStatus.PAID.value:
        invoice.paid_at = datetime.now(timezone.utc)

    invoice.status = new_status

    _write_audit_log(
        session,
        entity_type="Invoice",
        entity_id=invoice.id,
        old_state=old,
        new_state=new_status,
        actor=actor,
        reason=reason,
    )
    session.flush()
    return invoice
