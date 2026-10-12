"""
FS-CAE Layer 5 Automated Test Suite: Mobilization Invoicing, Payment Reconciliation,
Client Activation, Delivery Briefs, Follow-Up Automation & Full Commercial Spine E2E.
"""

from __future__ import annotations

import argparse
import csv
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.exceptions import (
    DuplicatePaymentError,
    InvalidStateTransitionError,
    UnmetMilestoneError,
)
from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.commercial import (
    Invoice,
    PaymentEvent,
    ProjectActivation,
    Proposal,
)
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.models.audit_log import AuditLog
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    ConversationState,
    DispatchStatus,
    InvoiceStatus,
    MilestoneTier,
    PackageType,
    ProjectActivationStatus,
    ProposalStatus,
    QualificationState,
)
from app.services.commercial.activation_service import (
    activate_client_project,
    compile_delivery_brief,
    format_delivery_brief_markdown,
)
from app.services.commercial.invoice_service import (
    create_milestone_invoice,
    format_invoice_markdown,
    generate_payment_instructions,
    generate_sequential_invoice_number,
)
from app.services.commercial.payment_service import reconcile_payment
from app.services.commercial.proposal_generator import (
    create_grand_slam_proposal,
    format_proposal_markdown,
    issue_proposal,
)
from app.services.commercial.audit_generator import (
    approve_commercial_audit,
    generate_commercial_audit,
)
from app.services.conversation.conversation_service import ConversationService
from app.services.ingestion.importer import import_file
from app.services.intelligence.gemini_client import GeminiAnalysisClient
from app.services.intelligence.key_manager import GeminiKeyManager
from app.services.intelligence.qualifier import qualify_business
from app.services.outreach.batch_manager import (
    approve_batch,
    draft_outreach_for_qualified,
)
from app.services.outreach.dispatcher import DispatchWorker
from app.services.outreach.followup_service import (
    cancel_pending_followups,
    evaluate_stalled_conversations_and_queue_followups,
    generate_followup_copy,
)
from app.services.state_machine import (
    transition_conversation,
    transition_proposal,
)


# =========================================================================== #
#  HELPERS / FIXTURES
# =========================================================================== #

def _create_biz(session, **kwargs) -> Business:
    name = kwargs.pop("name", "Kafue Earthmoving Ltd")
    defaults = dict(
        company_name=name,
        location_district="Kafue",
        industry_sector="Civil Earthmoving & Equipment Hire",
        phone_normalized="+260977112233",
        email="contracts@kafue.zm",
        fb_page_url="https://facebook.com/kafueearthmoving",
        source_type="INSTANT_DATA_SCRAPER",
        is_suppressed=False,
    )
    defaults.update(kwargs)
    biz = Business(**defaults)
    session.add(biz)
    session.commit()
    session.refresh(biz)
    return biz


def _create_qual(session, biz: Business, **kwargs) -> ProspectQualification:
    defaults = dict(
        business_id=biz.id,
        qualification_state=QualificationState.QUALIFIED.value,
        fit_score=85,
        sector_match=True,
        asset_footprint={"heavy_machinery": ["CAT 320D Excavator", "Komatsu PC200", "20-ton Tipper"]},
    )
    defaults.update(kwargs)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.commit()
    session.refresh(qual)
    return qual


def _create_issued_proposal(session, biz):
    proposal = create_grand_slam_proposal(session, biz.id)
    proposal = issue_proposal(session, proposal.id)
    return proposal


# =========================================================================== #
#  1. INVOICE CREATION & CONFIG HONESTY TESTS
# =========================================================================== #

class TestInvoiceCreationAndConfigHonesty:
    """Tests invoice creation, milestone pricing, sequential numbering, and config honesty."""

    def test_mobilization_invoice_generation(self, session, monkeypatch):
        # Configure verified bank details
        monkeypatch.setattr(
            "app.services.commercial.invoice_service.get_settings",
            lambda: Settings(
                FOUNDRY_LEGAL_NAME="Foundry Systems (Elysium Group Limited)",
                FOUNDRY_BANK_NAME="First National Bank (FNB)",
                FOUNDRY_ACCOUNT_NUMBER="62819283741",
                FOUNDRY_BRANCH_CODE="260001",
            ),
        )

        biz = _create_biz(session)
        _create_qual(session, biz)
        conv = Conversation(business_id=biz.id, channel="FB_MESSENGER", state="PROPOSAL_REQUESTED")
        session.add(conv)
        session.commit()

        proposal = _create_issued_proposal(session, biz)
        assert proposal.status == ProposalStatus.ISSUED.value

        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        assert invoice.invoice_number.startswith("INV-2026-")
        assert float(invoice.amount) == 5500.00
        assert invoice.milestone_tier == MilestoneTier.MOBILIZATION.value
        assert invoice.status == InvoiceStatus.ISSUED.value
        assert "First National Bank (FNB)" in invoice.payment_instructions
        assert "62819283741" in invoice.payment_instructions

        # Proposal transitioned to ACCEPTED
        session.refresh(proposal)
        assert proposal.status == ProposalStatus.ACCEPTED.value

        # ProjectActivation created in PENDING_MOBILIZATION
        activation = session.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).one()
        assert activation.status == ProjectActivationStatus.PENDING_MOBILIZATION.value
        assert activation.mobilization_invoice_id == invoice.id

        # Conversation transitioned to INVOICED
        session.refresh(conv)
        assert conv.state == ConversationState.INVOICED.value

    def test_mid_project_and_pre_launch_invoices(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)

        inv_mid = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MID_PROJECT.value)
        assert float(inv_mid.amount) == 4500.00
        assert inv_mid.milestone_tier == MilestoneTier.MID_PROJECT.value

        inv_launch = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.PRE_LAUNCH.value)
        assert float(inv_launch.amount) == 4500.00
        assert inv_launch.milestone_tier == MilestoneTier.PRE_LAUNCH.value

    def test_sequential_invoice_numbering_uniqueness(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)

        inv1 = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        inv2 = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MID_PROJECT.value)
        inv3 = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.PRE_LAUNCH.value)

        assert inv1.invoice_number == "INV-2026-0001"
        assert inv2.invoice_number == "INV-2026-0002"
        assert inv3.invoice_number == "INV-2026-0003"
        assert len({inv1.invoice_number, inv2.invoice_number, inv3.invoice_number}) == 3

    def test_bank_instructions_honest_placeholder_when_unconfigured(self, session, monkeypatch):
        # Empty bank credentials in settings
        monkeypatch.setattr(
            "app.services.commercial.invoice_service.get_settings",
            lambda: Settings(
                FOUNDRY_BANK_NAME=None,
                FOUNDRY_ACCOUNT_NUMBER=None,
            ),
        )

        instructions = generate_payment_instructions()
        assert instructions == "[PAYMENT INSTRUCTIONS PENDING OPERATOR CONFIGURATION]"

        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)
        inv = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        assert inv.payment_instructions == "[PAYMENT INSTRUCTIONS PENDING OPERATOR CONFIGURATION]"

    def test_draft_proposal_cannot_be_invoiced(self, session):
        biz = _create_biz(session)
        proposal = create_grand_slam_proposal(session, biz.id)
        assert proposal.status == ProposalStatus.DRAFT.value

        with pytest.raises(InvalidStateTransitionError):
            create_milestone_invoice(session, proposal.id)

    def test_invoice_markdown_rendering(self, session):
        biz = _create_biz(session, name="Mopani Plant Services Ltd")
        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        md = format_invoice_markdown(invoice, proposal, biz)
        assert "# COMMERCIAL TAX INVOICE" in md
        assert "Mopani Plant Services Ltd" in md
        assert invoice.invoice_number in md
        assert "K5,500.00" in md
        assert "TOTAL DUE:" in md


# =========================================================================== #
#  2. PAYMENT RECONCILIATION & INTEGRITY GATES TESTS
# =========================================================================== #

class TestPaymentReconciliationAndIntegrityGates:
    """Tests the non-negotiable payment reconciliation gate and duplicate guards."""

    def test_cannot_activate_project_with_unpaid_invoice(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        assert invoice.status == InvoiceStatus.ISSUED.value

        with pytest.raises(UnmetMilestoneError) as exc_info:
            activate_client_project(session, invoice.id)
        assert "strictly PAID" in str(exc_info.value)

    def test_reconcile_valid_payment_marks_paid_activates_client_and_wins_conversation(self, session):
        biz = _create_biz(session)
        conv = Conversation(business_id=biz.id, channel="FB_MESSENGER", state="INVOICED")
        session.add(conv)
        session.commit()

        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        # Reconcile valid payment
        event = reconcile_payment(
            session,
            invoice_id=invoice.id,
            amount_received=5500.00,
            transaction_reference="TX-FNB-2026-9988",
            verified_by="Lead Engineer",
            notes="Direct deposit confirmed on FNB statement",
        )

        assert event.id is not None
        assert event.transaction_reference == "TX-FNB-2026-9988"
        assert float(event.amount_received) == 5500.00

        # Invoice is PAID
        session.refresh(invoice)
        assert invoice.status == InvoiceStatus.PAID.value

        # ProjectActivation is ACTIVATED
        activation = session.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).one()
        assert activation.status == ProjectActivationStatus.ACTIVATED.value
        assert activation.mobilization_invoice_id == invoice.id

        # Conversation is WON
        session.refresh(conv)
        assert conv.state == ConversationState.WON

        # AuditLog recorded payment and activation
        logs = session.query(AuditLog).filter(AuditLog.entity_id == invoice.id).all()
        assert any("PAID" in (l.new_state or "") for l in logs)

    def test_duplicate_transaction_reference_rejected(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)
        inv1 = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        inv2 = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MID_PROJECT.value)

        reconcile_payment(
            session,
            invoice_id=inv1.id,
            amount_received=5500.00,
            transaction_reference="REF-DUP-001",
            verified_by="Auditor",
        )

        # Attempt to reuse the same ref for another invoice
        with pytest.raises(DuplicatePaymentError) as exc_info:
            reconcile_payment(
                session,
                invoice_id=inv2.id,
                amount_received=4500.00,
                transaction_reference="REF-DUP-001",
                verified_by="Auditor",
            )
        assert "REF-DUP-001" in str(exc_info.value)

    def test_insufficient_payment_amount_rejected(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        with pytest.raises(ValueError) as exc_info:
            reconcile_payment(
                session,
                invoice_id=invoice.id,
                amount_received=2000.00,  # Required 5,500
                transaction_reference="TX-SHORT-001",
                verified_by="Auditor",
            )
        assert "Insufficient payment amount" in str(exc_info.value)
        session.refresh(invoice)
        assert invoice.status == InvoiceStatus.ISSUED.value

    def test_overpayment_accepted(self, session):
        biz = _create_biz(session)
        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        event = reconcile_payment(
            session,
            invoice_id=invoice.id,
            amount_received=6000.00,  # Overpayment
            transaction_reference="TX-OVER-001",
            verified_by="Auditor",
        )
        session.refresh(invoice)
        assert invoice.status == InvoiceStatus.PAID.value
        assert float(event.amount_received) == 6000.00


# =========================================================================== #
#  3. DELIVERY BRIEF COMPILATION TESTS
# =========================================================================== #

class TestDeliveryBriefCompilation:
    """Tests compilation of the Builder.io Implementation Delivery Brief."""

    def test_compile_delivery_brief_structure_and_assets(self, session):
        biz = _create_biz(session, name="Kafue Earthmoving Ltd")
        qual = _create_qual(session, biz)
        audit = generate_commercial_audit(session, biz.id)
        proposal = _create_issued_proposal(session, biz)

        brief = compile_delivery_brief(session, biz.id)

        assert brief["client_name"] == "Kafue Earthmoving Ltd"
        assert brief["business_id"] == biz.id
        assert brief["package"] == PackageType.CAPABILITY_PORTAL_CORE.value
        assert brief["total_value"] == 14500.00
        assert brief["mobilization_paid"] is True
        assert "CAT 320D" in brief["verified_assets"]
        assert len(brief["audit_findings"]) == 3
        assert "Builder.io" not in brief["client_name"]  # sanity check

        # Master prompt assertions
        prompt = brief["builder_master_prompt"]
        assert "Kafue Earthmoving Ltd" in prompt
        assert "Kafue" in prompt
        assert "Hero Section" in prompt
        assert "Interactive Fleet Specification Matrix" in prompt
        assert "Commercial Procurement Verification Badge" in prompt
        assert "Instant RFQ Submission Flow" in prompt
        assert "Zero placeholder text" in prompt

    def test_format_delivery_brief_markdown(self, session):
        biz = _create_biz(session, name="Copperbelt Quarrying Co")
        _create_qual(session, biz)
        generate_commercial_audit(session, biz.id)
        _create_issued_proposal(session, biz)

        brief = compile_delivery_brief(session, biz.id)
        md = format_delivery_brief_markdown(brief)

        assert "# CLIENT IMPLEMENTATION DELIVERY BRIEF" in md
        assert "Copperbelt Quarrying Co" in md
        assert "ACTIVATED" in md
        assert "BUILDER.IO MASTER ARCHITECTURAL PROMPT" in md


# =========================================================================== #
#  4. FOLLOW-UP AUTOMATION & CANCELLATION TESTS
# =========================================================================== #

class TestFollowUpAutomationAndCancellation:
    """Tests context-aware follow-up scheduling, attempt limits, and auto-cancellation."""

    def test_stalled_awaiting_reply_queues_followup_after_72h(self, session):
        biz = _create_biz(session)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        # Stalled 75 hours ago
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=75)
        session.add(conv)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 1
        job = queued[0]
        assert job.business_id == biz.id
        assert job.idempotency_key == f"followup_{biz.id}_AWAITING_REPLY_1"
        assert job.status == DispatchStatus.QUEUED.value
        assert "checking in" in job.rendered_content.lower()

    def test_recent_conversation_not_queued(self, session):
        biz = _create_biz(session)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        # Active 24 hours ago (less than 72h threshold)
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=24)
        session.add(conv)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 0

    def test_stalled_audit_dispatched_queues_followup_after_48h(self, session):
        biz = _create_biz(session)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AUDIT_DISPATCHED.value,
        )
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=50)
        session.add(conv)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 1
        assert "3-point capability review" in queued[0].rendered_content

    def test_stalled_proposal_requested_queues_followup_after_72h(self, session):
        biz = _create_biz(session)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.PROPOSAL_REQUESTED.value,
        )
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=75)
        session.add(conv)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 1
        assert "proposal" in queued[0].rendered_content.lower()

    def test_stalled_invoiced_queues_followup_after_48h(self, session):
        biz = _create_biz(session)
        conv = Conversation(business_id=biz.id, channel="FB_MESSENGER", state="PROPOSAL_REQUESTED")
        session.add(conv)
        session.commit()

        proposal = _create_issued_proposal(session, biz)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)

        session.refresh(conv)
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=50)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 1
        assert invoice.invoice_number in queued[0].rendered_content

    def test_max_followups_per_stage_capped_at_2(self, session):
        biz = _create_biz(session)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=100)
        session.add(conv)

        # Seed 2 previous follow-up jobs
        now = datetime.now(timezone.utc)
        j1 = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status=DispatchStatus.SENT.value,
            recipient_target=biz.company_name,
            rendered_content="First bump",
            idempotency_key=f"followup_{biz.id}_AWAITING_REPLY_1",
            scheduled_for=now,
        )
        j2 = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status=DispatchStatus.SENT.value,
            recipient_target=biz.company_name,
            rendered_content="Second bump",
            idempotency_key=f"followup_{biz.id}_AWAITING_REPLY_2",
            scheduled_for=now,
        )
        session.add_all([j1, j2])
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 0

    def test_inbound_reply_cancels_pending_followups(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        session.add(conv)

        job = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status=DispatchStatus.QUEUED.value,
            recipient_target=biz.company_name,
            rendered_content="Checking in",
            idempotency_key=f"followup_{biz.id}_AWAITING_REPLY_1",
            scheduled_for=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()

        # Prospect replies
        service = ConversationService()
        service.handle_inbound_message(session, biz.id, "Yes, send the review")

        session.refresh(job)
        assert job.status == DispatchStatus.BLOCKED.value
        assert "responded" in (job.blocked_reason or "").lower()

    def test_opt_out_cancels_pending_followups_and_suppresses(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        session.add(conv)

        job = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status=DispatchStatus.QUEUED.value,
            recipient_target=biz.company_name,
            rendered_content="Checking in",
            idempotency_key=f"followup_{biz.id}_AWAITING_REPLY_1",
            scheduled_for=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()

        # Inbound opt-out
        service = ConversationService()
        service.handle_inbound_message(session, biz.id, "Stop messaging me")

        session.refresh(job)
        assert job.status == DispatchStatus.BLOCKED.value
        session.refresh(biz)
        assert biz.is_suppressed is True

    def test_suppressed_business_never_receives_followup(self, session):
        biz = _create_biz(session, is_suppressed=True)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=100)
        session.add(conv)
        session.commit()

        queued = evaluate_stalled_conversations_and_queue_followups(session)
        assert len(queued) == 0


# =========================================================================== #
#  5. LAYER 5 CLI INTEGRATION TESTS
# =========================================================================== #

class TestLayer5CLIIntegration:
    """Verifies invoice, payment, project, and followup CLI commands."""

    def test_cli_invoice_create(self, engine, monkeypatch):
        from app.cli import cmd_invoice_create

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        prop = _create_issued_proposal(sess, biz)
        prop_id = prop.id
        sess.close()

        args = argparse.Namespace(proposal_id=prop_id, tier="mobilization")
        cmd_invoice_create(args)

        check_sess = SessionFactory()
        inv = check_sess.query(Invoice).filter(Invoice.proposal_id == prop_id).one()
        assert inv.milestone_tier == MilestoneTier.MOBILIZATION.value
        assert float(inv.amount) == 5500.00
        check_sess.close()

    def test_cli_payment_verify(self, engine, monkeypatch):
        from app.cli import cmd_payment_verify

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        prop = _create_issued_proposal(sess, biz)
        inv = create_milestone_invoice(sess, prop.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        inv_id = inv.id
        sess.close()

        args = argparse.Namespace(
            invoice_id=inv_id,
            amount=5500.0,
            ref="CLI-TX-12345",
            by="Managing Partner",
            notes="Paid via mobile banking",
        )
        cmd_payment_verify(args)

        check_sess = SessionFactory()
        inv = check_sess.query(Invoice).filter(Invoice.id == inv_id).one()
        assert inv.status == InvoiceStatus.PAID.value
        act = check_sess.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).one()
        assert act.status == ProjectActivationStatus.ACTIVATED.value
        check_sess.close()

    def test_cli_project_brief(self, engine, monkeypatch):
        from app.cli import cmd_project_brief

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        _create_qual(sess, biz)
        _create_issued_proposal(sess, biz)
        biz_id = biz.id
        sess.close()

        args = argparse.Namespace(business_id=biz_id)
        # Verify it runs and outputs brief without throwing
        cmd_project_brief(args)

    def test_cli_followup_run(self, engine, monkeypatch):
        from app.cli import cmd_followup_run

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        conv = Conversation(
            business_id=biz.id,
            channel="FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
        )
        conv.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=90)
        sess.add(conv)
        sess.commit()
        sess.close()

        args = argparse.Namespace()
        cmd_followup_run(args)

        check_sess = SessionFactory()
        jobs = check_sess.query(DispatchJob).filter(DispatchJob.business_id == biz.id).all()
        assert len(jobs) == 1
        assert "followup_" in jobs[0].idempotency_key
        check_sess.close()


# =========================================================================== #
#  6. THE FULL END-TO-END COMMERCIAL SPINE ACCEPTANCE TEST
# =========================================================================== #

class TestFullCommercialSpineE2E:
    """
    Executes the complete end-to-end commercial acquisition-to-activation loop:
    Raw CSV Import -> Qualification -> Outreach Dispatch -> Inbound Reply ->
    Audit Delivery -> Grand Slam Proposal -> Mobilization Invoice ->
    Verified Payment Reconciliation -> Project Activation -> Delivery Brief ->
    Unbroken Audit Log Chain of Custody.
    """

    def test_full_e2e_acquisition_to_activation_loop(self, session, monkeypatch):
        # 0. Setup verified bank details
        monkeypatch.setattr(
            "app.services.commercial.invoice_service.get_settings",
            lambda: Settings(
                FOUNDRY_LEGAL_NAME="Foundry Systems (Elysium Group Limited)",
                FOUNDRY_BANK_NAME="First National Bank (FNB)",
                FOUNDRY_ACCOUNT_NUMBER="62819283741",
                FOUNDRY_BRANCH_CODE="260001",
            ),
        )

        # 1. RAW CSV IMPORT
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "kafue_earthmoving.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "location", "phone", "email", "website", "fb_page_url", "industry"])
                writer.writerow([
                    "Kafue Earthmoving Ltd",
                    "Plot 492 Industrial Area Kafue Zambia",
                    "+260977112233",
                    "contracts@kafue.zm",
                    "",
                    "https://facebook.com/kafueearthmoving",
                    "Earthmoving equipment hire, CAT 320D excavators, 20-ton tipper trucks, road grading in Kafue & Lusaka corridor",
                ])
            import_res = import_file(session, csv_path, source_type="INSTANT_DATA_SCRAPER")
            assert import_res.new_businesses == 1

        biz = session.query(Business).filter(Business.company_name == "Kafue Earthmoving Ltd").one()
        assert biz.is_suppressed is False
        assert biz.qualification.qualification_state == QualificationState.DISCOVERED.value

        # 2. QUALIFICATION PIPELINE (Layer 2)
        def _mock_gemini_response_qualified(prompt: str) -> dict:
            return {
                "sector_match": True,
                "primary_sector": "Civil Engineering & Earthmoving",
                "asset_footprint": ["CAT 320D Excavator", "20-ton Bell Tippers"],
                "digital_deficits": ["No mobile fleet catalogue", "No website"],
                "evidence_summary": "Company operates heavy machinery for road construction.",
                "confidence_score": 0.85,
            }

        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(km, analyze_fn=_mock_gemini_response_qualified)
        qual = session.query(ProspectQualification).filter(ProspectQualification.business_id == biz.id).one()
        q_state = qualify_business(session, biz, qual, client)
        assert q_state == QualificationState.QUALIFIED.value
        assert qual.fit_score >= 65
        assert "CAT 320D Excavator" in qual.asset_footprint

        # 3. DRAFT, APPROVE, AND DISPATCH OUTREACH (Layer 3)
        jobs = draft_outreach_for_qualified(session, limit=10)
        assert len(jobs) == 1
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        dispatch_report = worker.dispatch_queue(session, force=True)
        assert dispatch_report.dispatched == 1

        conv = session.query(Conversation).filter(Conversation.business_id == biz.id).one()
        assert conv.state == ConversationState.AWAITING_REPLY.value

        # 4. PROCESS SIMULATED INBOUND REPLY (Layer 4)
        conv_service = ConversationService()
        inbound = conv_service.handle_inbound_message(session, biz.id, "Yes, send the review")
        assert inbound["intent"] == "POSITIVE_INTEREST"

        session.refresh(conv)
        assert conv.state == ConversationState.AUDIT_PENDING_APPROVAL.value

        # 5. APPROVE AND DELIVER 3-POINT CAPABILITY AUDIT
        conv_service.approve_and_send_audit(session, conv.id)
        session.refresh(conv)
        assert conv.state == ConversationState.AUDIT_DISPATCHED.value

        # 6. ISSUE K14,500 GRAND SLAM PROPOSAL
        proposal = create_grand_slam_proposal(session, biz.id)
        proposal = issue_proposal(session, proposal.id)
        transition_conversation(
            session,
            conv,
            ConversationState.PROPOSAL_REQUESTED.value,
            actor="SALES_ENGINEER",
            reason="Delivered K14,500 proposal to executive team",
        )
        session.refresh(conv)
        assert conv.state == ConversationState.PROPOSAL_REQUESTED.value
        assert float(proposal.total_price) == 14500.00
        assert float(proposal.milestone_1_mobilization) == 5500.00

        # 7. GENERATE K5,500 MOBILIZATION INVOICE (Layer 5)
        invoice = create_milestone_invoice(session, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value)
        session.refresh(conv)
        assert conv.state == ConversationState.INVOICED.value
        assert invoice.invoice_number.startswith("INV-2026-")
        assert float(invoice.amount) == 5500.00
        assert invoice.status == InvoiceStatus.ISSUED.value

        activation = session.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).one()
        assert activation.status == ProjectActivationStatus.PENDING_MOBILIZATION.value

        # 8. RECONCILE VERIFIED PAYMENT (K5,500 with verified ref)
        payment_event = reconcile_payment(
            session,
            invoice_id=invoice.id,
            amount_received=5500.00,
            transaction_reference="FNB-ZM-998877",
            verified_by="Foundry Managing Partner",
            notes="Direct RTGS verified on FNB online corporate banking",
        )
        assert payment_event.id is not None

        # 9. ASSERTIONS ON FINAL WON & ACTIVATED STATE
        session.refresh(invoice)
        assert invoice.status == InvoiceStatus.PAID.value

        session.refresh(activation)
        assert activation.status == ProjectActivationStatus.ACTIVATED.value

        session.refresh(conv)
        assert conv.state == ConversationState.WON

        # 10. COMPILE IMPLEMENTATION DELIVERY BRIEF
        brief = compile_delivery_brief(session, biz.id)
        assert brief["client_name"] == "Kafue Earthmoving Ltd"
        assert brief["mobilization_paid"] is True
        assert "CAT 320D" in brief["verified_assets"]
        assert "Builder.io" not in brief["client_name"]
        assert "Hero Section" in brief["builder_master_prompt"]
        assert "Zero placeholder text" in brief["builder_master_prompt"]

        # 11. VERIFY UNBROKEN AUDIT LOG CHAIN OF CUSTODY
        logs = session.query(AuditLog).all()
        entities_logged = {l.entity_type for l in logs}
        assert "ProspectQualification" in entities_logged
        assert "Proposal" in entities_logged
        assert "Invoice" in entities_logged
        assert "ProjectActivation" in entities_logged
        assert "Conversation" in entities_logged

        # Confirm exact flow transitions logged
        actions = [f"{l.entity_type}:{l.old_state}->{l.new_state}" for l in logs if l.new_state]
        assert any("ProspectQualification:" in a and "->QUALIFIED" in a for a in actions)
        assert any("Conversation:" in a and "->AUDIT_PENDING_APPROVAL" in a for a in actions)
        assert any("Conversation:" in a and "->AUDIT_DISPATCHED" in a for a in actions)
        assert any("Proposal:" in a and "->ISSUED" in a for a in actions)
        assert any("Invoice:" in a and "->ISSUED" in a for a in actions)
        assert any("Invoice:" in a and "->PAID" in a for a in actions)
        assert any("ProjectActivation:" in a and "->ACTIVATED" in a for a in actions)
        assert any("Conversation:" in a and "->WON" in a for a in actions)
