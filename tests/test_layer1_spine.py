"""
Layer 1 Spine — Comprehensive Test Suite

Tests cover:
  1. Schema initialization
  2. Deterministic enum verification
  3. Valid qualification flow
  4. Invalid state transition enforcement
  5. Dispatch job lifecycle
  6. Financial integrity gates (payment, activation, duplicate detection)
  7. Audit log verifiability
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.exceptions import (
    DuplicatePaymentError,
    InvalidStateTransitionError,
    UnmetMilestoneError,
)
from app.models.audit_log import AuditLog
from app.models.business import Business
from app.models.commercial import Invoice, PaymentEvent, Proposal, ProjectActivation
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    CommercialAuditStatus,
    ConversationChannel,
    ConversationState,
    DeliveryStatus,
    DispatchChannel,
    DispatchStatus,
    InvoiceStatus,
    MessageDirection,
    MilestoneTier,
    PackageType,
    ProjectActivationStatus,
    ProposalStatus,
    QualificationState,
)
from app.services.state_machine import (
    activate_project,
    record_payment,
    transition_conversation,
    transition_dispatch,
    transition_qualification,
)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _make_business(session, **overrides) -> Business:
    defaults = dict(
        company_name="Test Corp",
        source_type="MANUAL",
        source_provenance={"origin": "test"},
    )
    defaults.update(overrides)
    biz = Business(**defaults)
    session.add(biz)
    session.flush()
    return biz


def _make_qualification(session, business: Business, **overrides) -> ProspectQualification:
    defaults = dict(
        business_id=business.id,
        qualification_state=QualificationState.DISCOVERED.value,
    )
    defaults.update(overrides)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.flush()
    return qual


def _make_dispatch_job(session, business: Business, **overrides) -> DispatchJob:
    defaults = dict(
        business_id=business.id,
        channel=DispatchChannel.EMAIL.value,
        status=DispatchStatus.DRAFTED.value,
        recipient_target="test@example.com",
        rendered_content="Hello!",
        idempotency_key=str(uuid.uuid4()),
        scheduled_for=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    job = DispatchJob(**defaults)
    session.add(job)
    session.flush()
    return job


def _make_conversation(session, business: Business, **overrides) -> Conversation:
    defaults = dict(
        business_id=business.id,
        channel=ConversationChannel.FB_MESSENGER.value,
        state=ConversationState.AWAITING_REPLY.value,
    )
    defaults.update(overrides)
    conv = Conversation(**defaults)
    session.add(conv)
    session.flush()
    return conv


def _make_full_financial_stack(session, business: Business):
    """Create Proposal → Invoice → ProjectActivation for testing."""
    proposal = Proposal(
        business_id=business.id,
        package_type=PackageType.CAPABILITY_PORTAL_CORE.value,
        total_price=14500.00,
        milestone_1_mobilization=5500.00,
        milestone_2_midway=4500.00,
        milestone_3_launch=4500.00,
        continuity_monthly_fee=1500.00,
        status=ProposalStatus.ACCEPTED.value,
    )
    session.add(proposal)
    session.flush()

    invoice = Invoice(
        proposal_id=proposal.id,
        invoice_number=f"INV-2026-{uuid.uuid4().hex[:4].upper()}",
        milestone_tier=MilestoneTier.MOBILIZATION.value,
        amount=5500.00,
        status=InvoiceStatus.ISSUED.value,
        due_date=datetime.now(timezone.utc),
    )
    session.add(invoice)
    session.flush()

    activation = ProjectActivation(
        business_id=business.id,
        proposal_id=proposal.id,
        mobilization_invoice_id=invoice.id,
        status=ProjectActivationStatus.PENDING_MOBILIZATION.value,
    )
    session.add(activation)
    session.flush()

    return proposal, invoice, activation


# =========================================================================== #
#  1. SCHEMA INITIALIZATION
# =========================================================================== #

class TestSchemaInitialization:
    """Verify all tables are created in an in-memory SQLite database."""

    def test_all_tables_exist(self, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        expected_tables = {
            "businesses",
            "prospect_qualifications",
            "dispatch_jobs",
            "conversations",
            "messages",
            "commercial_audits",
            "proposals",
            "invoices",
            "payment_events",
            "project_activations",
            "audit_logs",
        }
        actual_tables = set(inspector.get_table_names())
        assert expected_tables.issubset(actual_tables), (
            f"Missing tables: {expected_tables - actual_tables}"
        )

    def test_business_table_has_expected_columns(self, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("businesses")}
        required = {
            "id", "company_name", "phone_raw", "phone_normalized",
            "email", "website_url", "fb_page_url", "industry_sector",
            "location_district", "description_raw", "source_type",
            "source_provenance", "is_suppressed", "suppressed_reason",
            "created_at", "updated_at",
        }
        assert required.issubset(columns), f"Missing columns: {required - columns}"


# =========================================================================== #
#  2. DETERMINISTIC ENUM VERIFICATION
# =========================================================================== #

class TestDeterministicEnums:
    """Verify all state and channel enums exist with expected values."""

    def test_qualification_states(self):
        expected = {
            "DISCOVERED", "EVALUATING", "QUALIFIED",
            "DISQUALIFIED", "STAGED_FOR_OUTREACH", "SUPPRESSED",
        }
        actual = {s.value for s in QualificationState}
        assert actual == expected

    def test_dispatch_statuses(self):
        expected = {
            "DRAFTED", "QUEUED", "SENDING", "SENT",
            "DELIVERED", "BLOCKED", "FAILED",
        }
        actual = {s.value for s in DispatchStatus}
        assert actual == expected

    def test_dispatch_channels(self):
        expected = {"FB_MESSENGER", "EMAIL", "MANUAL"}
        actual = {c.value for c in DispatchChannel}
        assert actual == expected

    def test_conversation_states(self):
        expected = {
            "AWAITING_REPLY", "POSITIVE_REPLY", "OBJECTION_OR_QUESTION",
            "OPTED_OUT", "LOST", "AUDIT_PENDING_APPROVAL",
            "AUDIT_DISPATCHED", "OFFER_PENDING_APPROVAL",
            "OFFER_DISPATCHED", "PROPOSAL_REQUESTED", "INVOICED",
        }
        actual = {s.value for s in ConversationState}
        assert actual == expected

    def test_conversation_channels(self):
        expected = {"FB_MESSENGER", "EMAIL"}
        actual = {c.value for c in ConversationChannel}
        assert actual == expected

    def test_message_directions(self):
        expected = {"INBOUND", "OUTBOUND"}
        actual = {d.value for d in MessageDirection}
        assert actual == expected

    def test_delivery_statuses(self):
        expected = {"PENDING", "SENT", "RECEIVED", "FAILED"}
        actual = {s.value for s in DeliveryStatus}
        assert actual == expected

    def test_commercial_audit_statuses(self):
        expected = {"PENDING_GENERATION", "GENERATED", "APPROVED", "DELIVERED"}
        actual = {s.value for s in CommercialAuditStatus}
        assert actual == expected

    def test_package_types(self):
        expected = {"CAPABILITY_PORTAL_CORE", "LAUNCH_SYSTEM_BASIC"}
        actual = {p.value for p in PackageType}
        assert actual == expected

    def test_proposal_statuses(self):
        expected = {"DRAFT", "ISSUED", "ACCEPTED", "DECLINED", "EXPIRED"}
        actual = {s.value for s in ProposalStatus}
        assert actual == expected

    def test_milestone_tiers(self):
        expected = {"MOBILIZATION", "MID_PROJECT", "PRE_LAUNCH"}
        actual = {t.value for t in MilestoneTier}
        assert actual == expected

    def test_invoice_statuses(self):
        expected = {"DRAFT", "ISSUED", "PAYMENT_PENDING", "PAID", "VOID"}
        actual = {s.value for s in InvoiceStatus}
        assert actual == expected

    def test_project_activation_statuses(self):
        expected = {"PENDING_MOBILIZATION", "ACTIVATED", "IN_DELIVERY", "COMPLETED"}
        actual = {s.value for s in ProjectActivationStatus}
        assert actual == expected


# =========================================================================== #
#  3. VALID QUALIFICATION FLOW
# =========================================================================== #

class TestValidQualificationFlow:
    """Test the happy-path prospect qualification pipeline."""

    def test_discovered_to_evaluating(self, session):
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        transition_qualification(session, qual, QualificationState.EVALUATING.value)

        assert qual.qualification_state == QualificationState.EVALUATING.value

    def test_evaluating_to_qualified(self, session):
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.EVALUATING.value,
        )

        transition_qualification(
            session, qual, QualificationState.QUALIFIED.value, fit_score=80,
        )

        assert qual.qualification_state == QualificationState.QUALIFIED.value
        assert qual.fit_score == 80
        assert qual.evaluated_at is not None

    def test_qualified_to_staged(self, session):
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.QUALIFIED.value,
        )

        transition_qualification(
            session, qual, QualificationState.STAGED_FOR_OUTREACH.value,
        )

        assert qual.qualification_state == QualificationState.STAGED_FOR_OUTREACH.value

    def test_full_happy_path(self, session):
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        transition_qualification(session, qual, QualificationState.EVALUATING.value)
        transition_qualification(
            session, qual, QualificationState.QUALIFIED.value, fit_score=72,
        )
        transition_qualification(
            session, qual, QualificationState.STAGED_FOR_OUTREACH.value,
        )

        assert qual.qualification_state == QualificationState.STAGED_FOR_OUTREACH.value

    def test_evaluating_to_disqualified(self, session):
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.EVALUATING.value,
        )

        transition_qualification(
            session, qual,
            QualificationState.DISQUALIFIED.value,
            fit_score=30,
            disqualification_reason="Too small",
        )

        assert qual.qualification_state == QualificationState.DISQUALIFIED.value
        assert qual.fit_score == 30
        assert qual.disqualification_reason == "Too small"

    def test_any_state_to_suppressed(self, session):
        """Any qualification state can transition to SUPPRESSED."""
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.QUALIFIED.value,
        )

        transition_qualification(
            session, qual, QualificationState.SUPPRESSED.value,
            reason="Opt-out requested",
        )

        assert qual.qualification_state == QualificationState.SUPPRESSED.value


# =========================================================================== #
#  4. INVALID STATE TRANSITION ENFORCEMENT
# =========================================================================== #

class TestInvalidStateTransitions:
    """Verify that illegal transitions raise InvalidStateTransitionError."""

    def test_discovered_cannot_jump_to_staged(self, session):
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition_qualification(
                session, qual, QualificationState.STAGED_FOR_OUTREACH.value,
            )

        assert "DISCOVERED" in str(exc_info.value)
        assert "STAGED_FOR_OUTREACH" in str(exc_info.value)

    def test_discovered_cannot_jump_to_qualified(self, session):
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        with pytest.raises(InvalidStateTransitionError):
            transition_qualification(
                session, qual, QualificationState.QUALIFIED.value, fit_score=90,
            )

    def test_qualify_with_low_score_fails(self, session):
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.EVALUATING.value,
        )

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition_qualification(
                session, qual, QualificationState.QUALIFIED.value, fit_score=40,
            )

        assert "fit_score" in str(exc_info.value)

    def test_disqualify_with_high_score_fails(self, session):
        biz = _make_business(session)
        qual = _make_qualification(
            session, biz,
            qualification_state=QualificationState.EVALUATING.value,
        )

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition_qualification(
                session, qual, QualificationState.DISQUALIFIED.value, fit_score=80,
            )

        assert "fit_score" in str(exc_info.value)

    def test_conversation_awaiting_cannot_jump_to_audit_dispatched(self, session):
        biz = _make_business(session)
        conv = _make_conversation(session, biz)

        with pytest.raises(InvalidStateTransitionError):
            transition_conversation(
                session, conv, ConversationState.AUDIT_DISPATCHED.value,
            )

    def test_dispatch_drafted_cannot_jump_to_delivered(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz)

        with pytest.raises(InvalidStateTransitionError):
            transition_dispatch(session, job, DispatchStatus.DELIVERED.value)


# =========================================================================== #
#  5. DISPATCH JOB LIFECYCLE
# =========================================================================== #

class TestDispatchJobLifecycle:
    """Verify dispatch transitions including blocked and failed paths."""

    def test_happy_path_to_delivered(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz)

        transition_dispatch(session, job, DispatchStatus.QUEUED.value)
        transition_dispatch(session, job, DispatchStatus.SENDING.value)
        transition_dispatch(session, job, DispatchStatus.SENT.value)
        transition_dispatch(session, job, DispatchStatus.DELIVERED.value)

        assert job.status == DispatchStatus.DELIVERED.value
        assert job.sent_at is not None
        assert job.delivered_at is not None
        assert job.attempts == 1

    def test_blocked_requires_reason(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz)
        transition_dispatch(session, job, DispatchStatus.QUEUED.value)

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition_dispatch(session, job, DispatchStatus.BLOCKED.value)

        assert "blocked_reason" in str(exc_info.value)

    def test_blocked_with_reason_succeeds(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz)
        transition_dispatch(session, job, DispatchStatus.QUEUED.value)

        transition_dispatch(
            session, job, DispatchStatus.BLOCKED.value,
            blocked_reason="Platform policy forbids cold messaging",
        )

        assert job.status == DispatchStatus.BLOCKED.value
        assert job.blocked_reason == "Platform policy forbids cold messaging"

    def test_failed_can_requeue(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz, max_attempts=3)
        transition_dispatch(session, job, DispatchStatus.QUEUED.value)
        transition_dispatch(session, job, DispatchStatus.SENDING.value)
        transition_dispatch(
            session, job, DispatchStatus.FAILED.value,
            last_error="Connection timeout",
        )

        # Re-queue allowed because attempts (1) < max_attempts (3)
        transition_dispatch(session, job, DispatchStatus.QUEUED.value)
        assert job.status == DispatchStatus.QUEUED.value

    def test_failed_requeue_blocked_at_max_attempts(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz, max_attempts=1)
        transition_dispatch(session, job, DispatchStatus.QUEUED.value)
        transition_dispatch(session, job, DispatchStatus.SENDING.value)  # attempts → 1
        transition_dispatch(session, job, DispatchStatus.FAILED.value)

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition_dispatch(session, job, DispatchStatus.QUEUED.value)

        assert "max_attempts" in str(exc_info.value)


# =========================================================================== #
#  6. FINANCIAL INTEGRITY GATE
# =========================================================================== #

class TestFinancialIntegrityGate:
    """Verify payment verification, activation gating, and duplicate detection."""

    def test_activate_fails_when_invoice_not_paid(self, session):
        biz = _make_business(session)
        _proposal, invoice, activation = _make_full_financial_stack(session, biz)

        assert invoice.status == InvoiceStatus.ISSUED.value

        with pytest.raises(UnmetMilestoneError) as exc_info:
            activate_project(session, activation)

        assert "PAID" in str(exc_info.value)

    def test_payment_and_successful_activation(self, session):
        biz = _make_business(session)
        _proposal, invoice, activation = _make_full_financial_stack(session, biz)

        # Record payment
        event = record_payment(
            session,
            invoice,
            amount_received=5500.00,
            transaction_reference="TXN-ABCD-1234",
            verified_by="operator@foundry.zm",
        )
        assert event is not None
        assert invoice.status == InvoiceStatus.PAID.value
        assert invoice.paid_at is not None

        # Now activate
        activate_project(session, activation)
        assert activation.status == ProjectActivationStatus.ACTIVATED.value
        assert activation.activated_at is not None

    def test_duplicate_transaction_reference_raises(self, session):
        biz = _make_business(session)
        _proposal, invoice, _activation = _make_full_financial_stack(session, biz)

        record_payment(
            session,
            invoice,
            amount_received=5500.00,
            transaction_reference="TXN-UNIQUE-001",
            verified_by="operator@foundry.zm",
        )

        # Second business + invoice to attempt duplicate
        biz2 = _make_business(session, company_name="Other Corp")
        _proposal2, invoice2, _activation2 = _make_full_financial_stack(session, biz2)

        with pytest.raises(DuplicatePaymentError) as exc_info:
            record_payment(
                session,
                invoice2,
                amount_received=5500.00,
                transaction_reference="TXN-UNIQUE-001",
                verified_by="operator@foundry.zm",
            )

        assert "TXN-UNIQUE-001" in str(exc_info.value)

    def test_duplicate_reference_also_caught_by_db_constraint(self, session):
        """Even if service-level check is bypassed, DB unique constraint fires."""
        biz = _make_business(session)
        _proposal, invoice, _act = _make_full_financial_stack(session, biz)

        now = datetime.now(timezone.utc)
        pe1 = PaymentEvent(
            invoice_id=invoice.id,
            amount_received=5500.00,
            transaction_reference="TXN-DB-CONSTRAINT",
            verified_by="admin",
            verified_at=now,
        )
        session.add(pe1)
        session.flush()

        pe2 = PaymentEvent(
            invoice_id=invoice.id,
            amount_received=5500.00,
            transaction_reference="TXN-DB-CONSTRAINT",
            verified_by="admin",
            verified_at=now,
        )
        session.add(pe2)

        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# =========================================================================== #
#  7. AUDIT LOG VERIFIABILITY
# =========================================================================== #

class TestAuditLogVerifiability:
    """Confirm that every tested transition creates an immutable AuditLog entry."""

    def test_qualification_transition_creates_audit_log(self, session):
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        transition_qualification(session, qual, QualificationState.EVALUATING.value)

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "ProspectQualification",
            AuditLog.entity_id == qual.id,
        ).all()
        assert len(logs) == 1
        assert logs[0].old_state == QualificationState.DISCOVERED.value
        assert logs[0].new_state == QualificationState.EVALUATING.value

    def test_dispatch_transition_creates_audit_log(self, session):
        biz = _make_business(session)
        job = _make_dispatch_job(session, biz)

        transition_dispatch(session, job, DispatchStatus.QUEUED.value)
        transition_dispatch(session, job, DispatchStatus.SENDING.value)

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "DispatchJob",
            AuditLog.entity_id == job.id,
        ).order_by(AuditLog.created_at).all()
        assert len(logs) == 2
        assert logs[0].new_state == DispatchStatus.QUEUED.value
        assert logs[1].new_state == DispatchStatus.SENDING.value

    def test_conversation_transition_creates_audit_log(self, session):
        biz = _make_business(session)
        conv = _make_conversation(session, biz)

        transition_conversation(
            session, conv, ConversationState.POSITIVE_REPLY.value,
        )

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "Conversation",
            AuditLog.entity_id == conv.id,
        ).all()
        assert len(logs) == 1
        assert logs[0].old_state == ConversationState.AWAITING_REPLY.value
        assert logs[0].new_state == ConversationState.POSITIVE_REPLY.value

    def test_payment_creates_invoice_audit_log(self, session):
        biz = _make_business(session)
        _proposal, invoice, _activation = _make_full_financial_stack(session, biz)

        record_payment(
            session,
            invoice,
            amount_received=5500.00,
            transaction_reference="TXN-AUDIT-CHECK",
            verified_by="operator",
        )

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "Invoice",
            AuditLog.entity_id == invoice.id,
        ).all()
        assert len(logs) == 1
        assert logs[0].new_state == InvoiceStatus.PAID.value

    def test_project_activation_creates_audit_log(self, session):
        biz = _make_business(session)
        _proposal, invoice, activation = _make_full_financial_stack(session, biz)

        record_payment(
            session, invoice,
            amount_received=5500.00,
            transaction_reference="TXN-ACTIVATE-AUDIT",
            verified_by="operator",
        )
        activate_project(session, activation)

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "ProjectActivation",
            AuditLog.entity_id == activation.id,
        ).all()
        assert len(logs) == 1
        assert logs[0].old_state == ProjectActivationStatus.PENDING_MOBILIZATION.value
        assert logs[0].new_state == ProjectActivationStatus.ACTIVATED.value

    def test_multiple_transitions_create_ordered_log(self, session):
        """Full qualification pipeline produces a complete audit trail."""
        biz = _make_business(session)
        qual = _make_qualification(session, biz)

        transition_qualification(session, qual, QualificationState.EVALUATING.value)
        transition_qualification(
            session, qual, QualificationState.QUALIFIED.value, fit_score=85,
        )
        transition_qualification(
            session, qual, QualificationState.STAGED_FOR_OUTREACH.value,
        )

        logs = session.query(AuditLog).filter(
            AuditLog.entity_type == "ProspectQualification",
            AuditLog.entity_id == qual.id,
        ).order_by(AuditLog.created_at).all()

        assert len(logs) == 3
        assert [l.new_state for l in logs] == [
            QualificationState.EVALUATING.value,
            QualificationState.QUALIFIED.value,
            QualificationState.STAGED_FOR_OUTREACH.value,
        ]
