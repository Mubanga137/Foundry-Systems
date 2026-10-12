"""
FS-CAE Layer 4 Comprehensive Test Suite
Conversation Engine, 3-Point Audits & Grand Slam Proposals

Tests cover:
  1. Intent Classification (7 intents, fast-path regex, opt-out priority, Gemini fallback)
  2. Sector-Specific 3-Point Capability Audits (Contractors, Plant Hire, Quarries, Logistics)
  3. Grand Slam Proposals & Financial Mathematics (K14,500 total, 38/31/31 split, K1,500/mo retainer)
  4. Conversation Lifecycle & State Transitions (AWAITING_REPLY -> POSITIVE_REPLY -> AUDIT_PENDING_APPROVAL -> AUDIT_DISPATCHED)
  5. Objection Handlers (Pricing anchor K35,000, existing website rebuttal, call requests)
  6. Suppression Enforcement on Opt-Out (is_suppressed=True, cancellation of queued jobs, audit log)
  7. CLI Commands Integration
"""

from __future__ import annotations

import argparse
from datetime import datetime as dt, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.commercial import Proposal
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
    InboundIntent,
    MessageDirection,
    PackageType,
    ProposalStatus,
    QualificationState,
)
from app.services.commercial.audit_generator import (
    SECTOR_EARTHWORKS,
    SECTOR_LOGISTICS,
    SECTOR_PLANT_HIRE,
    SECTOR_QUARRY,
    approve_commercial_audit,
    detect_sector_category,
    format_audit_markdown,
    generate_commercial_audit,
)
from app.services.commercial.proposal_generator import (
    CORE_CONTINUITY_RETAINER,
    CORE_MILESTONE_1_MOBILIZATION,
    CORE_MILESTONE_2_MIDWAY,
    CORE_MILESTONE_3_LAUNCH,
    CORE_TOTAL_PRICE,
    create_grand_slam_proposal,
    format_proposal_markdown,
    issue_proposal,
)
from app.services.conversation.classifier import classify_inbound_intent
from app.services.conversation.conversation_service import ConversationService
from app.services.conversation.response_generator import (
    generate_call_or_meeting_response,
    generate_existing_website_response,
    generate_general_question_response,
    generate_pricing_response,
    generate_timing_delay_response,
)
from app.services.state_machine import transition_audit, transition_conversation, transition_proposal


# --------------------------------------------------------------------------- #
#  Helpers & Fixtures
# --------------------------------------------------------------------------- #

def _create_biz(session, **kwargs) -> Business:
    defaults = dict(
        company_name="Zambezi Civil & Earthworks Ltd",
        location_district="Kafue Road, Lusaka",
        industry_sector="Civil Earthmoving & Road Construction",
        phone_normalized="+260977112233",
        email="contracts@zambezicivil.zm",
        fb_page_url="https://facebook.com/zambezicivil",
        source_type="INSTANT_DATA_SCRAPER",
        is_suppressed=False,
    )
    defaults.update(kwargs)
    biz = Business(**defaults)
    session.add(biz)
    session.flush()
    return biz


def _create_qual(session, biz: Business, **kwargs) -> ProspectQualification:
    defaults = dict(
        business_id=biz.id,
        qualification_state=QualificationState.QUALIFIED.value,
        fit_score=85,
        sector_match=True,
        asset_footprint={"heavy_machinery": ["CAT 330 Excavator", "20-ton Tipper"]},
    )
    defaults.update(kwargs)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.flush()
    return qual


# =========================================================================== #
#  1. INTENT CLASSIFICATION TESTS
# =========================================================================== #

class TestIntentClassification:
    """Verifies intent routing across all 7 categories and opt-out priority."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "Yes, send it",
            "Sure, send through",
            "Go ahead",
            "I'd like to see it",
            "Please send the review",
            "Sounds good",
            "Yes please",
            "ok please send",
            "Yes",
            "Sure",
        ],
    )
    def test_positive_interest_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.POSITIVE_INTEREST

    @pytest.mark.parametrize(
        "phrase",
        [
            "How much do you charge?",
            "What's the cost?",
            "Send pricing please",
            "What are your rates?",
            "Quotation please",
            "How much is this service?",
            "What does it cost?",
        ],
    )
    def test_request_pricing_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.REQUEST_PRICING

    @pytest.mark.parametrize(
        "phrase",
        [
            "We already have a website",
            "Check our site at www.contractor.zm",
            "Already got a site thanks",
            "We have a website already",
            "Our website is live",
        ],
    )
    def test_has_existing_website_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.HAS_EXISTING_WEBSITE

    @pytest.mark.parametrize(
        "phrase",
        [
            "Call me on 0977123456",
            "Can we meet at our office in Kitwe?",
            "Give me a call tomorrow",
            "Phone me to discuss",
            "Ring me this afternoon",
        ],
    )
    def test_call_or_meeting_request_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.CALL_OR_MEETING_REQUEST

    @pytest.mark.parametrize(
        "phrase",
        [
            "Contact me next month",
            "Busy right now, check back later",
            "Not now, call back next week",
            "Please follow up later",
        ],
    )
    def test_timing_delay_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.TIMING_DELAY

    @pytest.mark.parametrize(
        "phrase",
        [
            "Stop",
            "Unsubscribe",
            "Remove me",
            "Not interested",
            "Don't message again",
            "Leave me alone",
            "No thanks",
            "No",
        ],
    )
    def test_opt_out_phrases(self, phrase):
        assert classify_inbound_intent(phrase) == InboundIntent.OPT_OUT

    def test_opt_out_precedence_over_other_keywords(self):
        # A message saying "No, stop sending your pricing" contains "pricing", but must classify as OPT_OUT
        msg = "No, stop sending your pricing"
        assert classify_inbound_intent(msg) == InboundIntent.OPT_OUT

    def test_general_question_fallback(self):
        msg = "Where are your headquarters located in Lusaka?"
        assert classify_inbound_intent(msg) == InboundIntent.GENERAL_QUESTION

    def test_gemini_client_fallback_when_provided(self):
        mock_client = MagicMock()
        mock_client.generate_text.return_value = "POSITIVE_INTEREST"

        # Ambiguous phrase that might not hit regex
        res = classify_inbound_intent("I am open to exploring this further", gemini_client=mock_client)
        assert res == InboundIntent.POSITIVE_INTEREST
        mock_client.generate_text.assert_called_once()

    def test_gemini_client_error_falls_back_to_general_question(self):
        mock_client = MagicMock()
        mock_client.generate_text.side_effect = RuntimeError("API unavailable")

        res = classify_inbound_intent("Some ambiguous text", gemini_client=mock_client)
        assert res == InboundIntent.GENERAL_QUESTION


# =========================================================================== #
#  2. SECTOR-SPECIFIC 3-POINT CAPABILITY AUDITS TESTS
# =========================================================================== #

class TestSectorSpecificAudits:
    """Verifies audits produce sector-grounded findings across all 4 categories."""

    def test_earthworks_and_civil_audit(self, session):
        biz = _create_biz(
            session,
            company_name="Kafue Earthworks Ltd",
            industry_sector="Civil Engineering & Road Construction",
        )
        _create_qual(session, biz)

        audit = generate_commercial_audit(session, biz.id)

        assert audit.status == CommercialAuditStatus.GENERATED.value
        assert "Technical Plant & Machine Tonnage" in audit.finding_1
        assert "Mobile RFQ Capture" in audit.finding_2
        assert "Compliance Credentials" in audit.finding_3
        assert "NCC registration" in audit.finding_3
        assert "Foundry Systems Capability Portal Core" in audit.recommended_scope

    def test_plant_hire_and_heavy_equipment_audit(self, session):
        biz = _create_biz(
            session,
            company_name="Copperbelt Plant Hire Ltd",
            industry_sector="Heavy Equipment & Crane Hire",
        )
        _create_qual(session, biz)

        audit = generate_commercial_audit(session, biz.id)

        assert "Equipment Catalogue Organization" in audit.finding_1
        assert "Request Instant Equipment Dispatch" in audit.finding_2
        assert "Mobilization Terms" in audit.finding_3

    def test_quarries_and_aggregates_audit(self, session):
        biz = _create_biz(
            session,
            company_name="Lusaka Aggregate Quarrying Plc",
            industry_sector="Stone Crushing, Aggregates & Sand",
        )
        _create_qual(session, biz)

        audit = generate_commercial_audit(session, biz.id)

        assert "Presentation of Material Grades" in audit.finding_1
        assert "G1–G7 gravel" in audit.finding_1
        assert "Bulk Volume Estimator" in audit.finding_2
        assert "Weighbridge Operations" in audit.finding_3

    def test_heavy_logistics_and_haulage_audit(self, session):
        biz = _create_biz(
            session,
            company_name="Chingola Haulage & Freight Ltd",
            industry_sector="Cross-Border Heavy Haulage & Logistics",
        )
        _create_qual(session, biz)

        audit = generate_commercial_audit(session, biz.id)

        assert "Route Coverage & Fleet Configurations" in audit.finding_1
        assert "GIT (Goods In Transit) Insurance" in audit.finding_2
        assert "Dispatch Communication Pathways" in audit.finding_3

    def test_format_audit_markdown_structure(self, session):
        biz = _create_biz(session)
        audit = generate_commercial_audit(session, biz.id)

        md = format_audit_markdown(audit, biz)

        assert "# 3-POINT COMMERCIAL CAPABILITY AUDIT" in md
        assert biz.company_name in md
        assert "## 1. OBSERVED CAPABILITY GAPS" in md
        assert "## 2. COMMERCIAL IMPLICATIONS" in md
        assert "## 3. RECOMMENDED RESOLUTION SCOPE" in md

    def test_approve_audit_transition(self, session):
        biz = _create_biz(session)
        audit = generate_commercial_audit(session, biz.id)
        assert audit.status == CommercialAuditStatus.GENERATED.value

        approved = approve_commercial_audit(session, audit.id)
        assert approved.status == CommercialAuditStatus.APPROVED.value


# =========================================================================== #
#  3. GRAND SLAM PROPOSALS & FINANCIAL MATHEMATICS TESTS
# =========================================================================== #

class TestGrandSlamProposalAndFinancials:
    """Verifies K14,500 total, 38/31/31 split math, line-itemed value stack, and issuing."""

    def test_mathematical_verification_of_38_31_31_split(self, session):
        biz = _create_biz(session)
        proposal = create_grand_slam_proposal(session, biz.id)

        # Exact financial values
        assert float(proposal.total_price) == CORE_TOTAL_PRICE
        assert float(proposal.milestone_1_mobilization) == CORE_MILESTONE_1_MOBILIZATION
        assert float(proposal.milestone_2_midway) == CORE_MILESTONE_2_MIDWAY
        assert float(proposal.milestone_3_launch) == CORE_MILESTONE_3_LAUNCH

        # Verification of the mathematical split: 5500 + 4500 + 4500 == 14500
        total_sum = (
            float(proposal.milestone_1_mobilization)
            + float(proposal.milestone_2_midway)
            + float(proposal.milestone_3_launch)
        )
        assert total_sum == float(proposal.total_price)
        assert total_sum == 14500.00

        # Percentage ratios
        pct_m1 = float(proposal.milestone_1_mobilization) / float(proposal.total_price)
        pct_m2 = float(proposal.milestone_2_midway) / float(proposal.total_price)
        pct_m3 = float(proposal.milestone_3_launch) / float(proposal.total_price)
        assert round(pct_m1, 2) == 0.38
        assert round(pct_m2, 2) == 0.31
        assert round(pct_m3, 2) == 0.31

        # Retainer fee
        assert float(proposal.continuity_monthly_fee) == CORE_CONTINUITY_RETAINER
        assert float(proposal.continuity_monthly_fee) == 1500.00

    def test_proposal_markdown_line_itemed_value_stack(self, session):
        biz = _create_biz(session)
        proposal = create_grand_slam_proposal(session, biz.id)

        md = format_proposal_markdown(proposal, biz)

        assert "COMMERCIAL PROPOSAL & SPECIFICATION" in md
        assert "TOTAL COMMERCIAL VALUE: .............................. K37,500" in md
        assert "DISCOUNTED PORTFOLIO INVESTMENT: .................... K14,500.00" in md
        assert "Milestone 1" in md and "K5,500.00" in md
        assert "Milestone 2" in md and "K4,500.00" in md
        assert "Milestone 3" in md and "K4,500.00" in md
        assert "K1,500.00 / month" in md

    def test_basic_alternative_package(self, session):
        biz = _create_biz(session)
        proposal = create_grand_slam_proposal(
            session, biz.id, package_type=PackageType.LAUNCH_SYSTEM_BASIC.value
        )

        assert float(proposal.total_price) == 5500.00
        assert float(proposal.milestone_1_mobilization) == 5500.00
        assert float(proposal.milestone_2_midway) == 0.00
        assert float(proposal.milestone_3_launch) == 0.00

    def test_issue_proposal_state_transition(self, session):
        biz = _create_biz(session)
        proposal = create_grand_slam_proposal(session, biz.id)
        assert proposal.status == ProposalStatus.DRAFT.value
        assert proposal.issued_at is None

        issued = issue_proposal(session, proposal.id)
        assert issued.status == ProposalStatus.ISSUED.value
        assert issued.issued_at is not None


# =========================================================================== #
#  4. CONVERSATION LIFECYCLE & STATE MACHINE TESTS
# =========================================================================== #

class TestConversationLifecycle:
    """Verifies end-to-end conversation state transitions and message logging."""

    def test_positive_interest_transitions_to_audit_pending_approval(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        service = ConversationService()

        # Inbound "Yes, send it"
        res = service.handle_inbound_message(session, biz.id, "Yes, send it please")

        assert res["intent"] == InboundIntent.POSITIVE_INTEREST.value
        assert res["new_state"] == ConversationState.AUDIT_PENDING_APPROVAL.value
        assert res["audit_id"] is not None

        # Verify Audit was generated in GENERATED state
        audit = session.query(CommercialAudit).filter(CommercialAudit.id == res["audit_id"]).one()
        assert audit.status == CommercialAuditStatus.GENERATED.value

        # Verify Conversation record
        conv = session.query(Conversation).filter(Conversation.business_id == biz.id).one()
        assert conv.state == ConversationState.AUDIT_PENDING_APPROVAL.value

        # Verify Inbound Message was logged
        msg = session.query(Message).filter(Message.conversation_id == conv.id).first()
        assert msg.direction == MessageDirection.INBOUND.value
        assert msg.content == "Yes, send it please"

    def test_approve_and_send_audit_dispatches_and_logs_outbound(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        service = ConversationService()

        # Step 1: Inbound Positive
        res = service.handle_inbound_message(session, biz.id, "Sure, go ahead")
        conv_id = res["conversation_id"]

        # Step 2: Operator approves audit
        delivery_res = service.approve_and_send_audit(session, conv_id)

        assert delivery_res["status"] == ConversationState.AUDIT_DISPATCHED.value

        # Conversation in AUDIT_DISPATCHED
        conv = session.query(Conversation).filter(Conversation.id == conv_id).one()
        assert conv.state == ConversationState.AUDIT_DISPATCHED.value

        # CommercialAudit in DELIVERED with delivered_at set
        audit = session.query(CommercialAudit).filter(CommercialAudit.id == delivery_res["audit_id"]).one()
        assert audit.status == CommercialAuditStatus.DELIVERED.value
        assert audit.delivered_at is not None

        # Outbound Message logged
        out_msg = (
            session.query(Message)
            .filter(
                Message.conversation_id == conv_id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
            .first()
        )
        assert out_msg is not None
        assert "3-POINT COMMERCIAL CAPABILITY AUDIT" in out_msg.content

    def test_request_pricing_moves_to_offer_pending_approval_with_anchor(self, session):
        biz = _create_biz(session)
        service = ConversationService()

        res = service.handle_inbound_message(session, biz.id, "How much do you charge?")

        assert res["intent"] == InboundIntent.REQUEST_PRICING.value
        assert res["new_state"] == ConversationState.OFFER_PENDING_APPROVAL.value
        assert "K35,000+" in res["suggested_reply"]
        assert "K14,500" in res["suggested_reply"]
        assert "K5,500 mobilization" in res["suggested_reply"]

    def test_has_existing_website_moves_to_objection_with_rebuttal(self, session):
        biz = _create_biz(session)
        service = ConversationService()

        res = service.handle_inbound_message(session, biz.id, "We already have a website")

        assert res["intent"] == InboundIntent.HAS_EXISTING_WEBSITE.value
        assert res["new_state"] == ConversationState.OBJECTION_OR_QUESTION.value
        assert "site estimators and procurement officers" in res["suggested_reply"]
        assert "WhatsApp RFQ capture" in res["suggested_reply"]

    def test_issue_proposal_for_conversation_flow(self, session):
        biz = _create_biz(session)
        service = ConversationService()

        conv = service.get_or_create_conversation(session, biz.id)
        # Advance conversation to OFFER_DISPATCHED
        transition_conversation(session, conv, ConversationState.OFFER_PENDING_APPROVAL.value)
        transition_conversation(session, conv, ConversationState.OFFER_DISPATCHED.value)

        # Issue Grand Slam Proposal
        proposal = service.issue_proposal_for_conversation(session, conv.id)

        assert proposal.status == ProposalStatus.ISSUED.value
        assert conv.state == ConversationState.PROPOSAL_REQUESTED.value

        # Verify outbound message logged
        out_msg = (
            session.query(Message)
            .filter(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        assert out_msg is not None
        assert "COMMERCIAL PROPOSAL & SPECIFICATION" in out_msg.content


# =========================================================================== #
#  5. SUPPRESSION ON OPT-OUT TESTS
# =========================================================================== #

class TestSuppressionOnOptOut:
    """Verifies immediate suppression, job cancellation, and audit trail on opt-out."""

    def test_opt_out_suppresses_business_and_cancels_jobs(self, session):
        biz = _create_biz(session, is_suppressed=False)
        service = ConversationService()

        # Queue a pending dispatch job
        now = dt.now(timezone.utc)
        job = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status=DispatchStatus.QUEUED.value,
            recipient_target="test_target",
            rendered_content="Test outreach copy",
            idempotency_key="opt_out_test_key",
            scheduled_for=now,
        )
        session.add(job)
        session.commit()

        # Inbound opt-out message
        res = service.handle_inbound_message(session, biz.id, "Stop messaging me")

        assert res["intent"] == InboundIntent.OPT_OUT.value
        assert res["new_state"] == ConversationState.OPTED_OUT.value

        # Verify business suppression
        session.refresh(biz)
        assert biz.is_suppressed is True
        assert biz.suppressed_reason == "Prospect requested opt-out"

        # Verify DispatchJob cancelled to BLOCKED
        session.refresh(job)
        assert job.status == DispatchStatus.BLOCKED.value
        assert "opted out" in job.blocked_reason.lower()

        # Verify Conversation state
        conv = session.query(Conversation).filter(Conversation.business_id == biz.id).one()
        assert conv.state == ConversationState.OPTED_OUT.value


# =========================================================================== #
#  6. OBJECTION HANDLERS INDEPENDENT TESTS
# =========================================================================== #

class TestResponseGenerators:
    """Verifies response text content for all objection types."""

    def test_pricing_response_anchoring(self, session):
        biz = _create_biz(session, company_name="Lusaka Earthworks")
        reply = generate_pricing_response(biz)
        assert "K35,000+" in reply
        assert "K14,500" in reply
        assert "K5,500" in reply

    def test_website_objection_reply_content(self, session):
        biz = _create_biz(session)
        reply = generate_existing_website_response(biz)
        assert "corporate landing page" in reply
        assert "mobile tool" in reply
        assert "machinery spec verification" in reply

    def test_call_or_meeting_response_content(self, session):
        biz = _create_biz(session, phone_normalized="+260977889900")
        reply = generate_call_or_meeting_response(biz)
        assert "+260977889900" in reply
        assert "brief 10-minute briefing" in reply

    def test_timing_delay_response_content(self, session):
        biz = _create_biz(session)
        reply = generate_timing_delay_response(biz)
        assert "next month" in reply

    def test_general_question_response_content(self, session):
        biz = _create_biz(session)
        reply = generate_general_question_response(biz)
        assert "Foundry Systems" in reply or "capability portals" in reply


# =========================================================================== #
#  7. CLI INTEGRATION TESTS
# =========================================================================== #

class TestLayer4CLIIntegration:
    """Verifies CLI subcommands: conversation reply/list, audit generate, proposal create."""

    def test_cli_conversation_reply(self, engine, monkeypatch):
        from app.cli import cmd_conversation_reply
        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        _create_qual(sess, biz)
        sess.commit()
        sess.close()

        # Run CLI command
        args = argparse.Namespace(
            business_id=biz.id,
            text="Yes, send it",
            channel="FB_MESSENGER",
        )
        cmd_conversation_reply(args)

        check_sess = SessionFactory()
        conv = check_sess.query(Conversation).filter(Conversation.business_id == biz.id).one()
        assert conv.state == ConversationState.AUDIT_PENDING_APPROVAL.value
        check_sess.close()

    def test_cli_conversation_list(self, engine, monkeypatch):
        from app.cli import cmd_conversation_list
        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        conv = Conversation(business_id=biz.id, channel="FB_MESSENGER", state="AWAITING_REPLY")
        sess.add(conv)
        sess.commit()
        sess.close()

        args = argparse.Namespace(limit=10)
        # Should execute without error
        cmd_conversation_list(args)

    def test_cli_audit_generate(self, engine, monkeypatch):
        from app.cli import cmd_audit_generate
        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        _create_qual(sess, biz)
        sess.commit()
        sess.close()

        args = argparse.Namespace(business_id=biz.id)
        cmd_audit_generate(args)

        check_sess = SessionFactory()
        audit = check_sess.query(CommercialAudit).filter(CommercialAudit.business_id == biz.id).one()
        assert audit.status == CommercialAuditStatus.GENERATED.value
        check_sess.close()

    def test_cli_proposal_create_and_issue(self, engine, monkeypatch):
        from app.cli import cmd_proposal_create
        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        sess = SessionFactory()
        biz = _create_biz(sess)
        sess.commit()
        sess.close()

        args = argparse.Namespace(business_id=biz.id, package="core", issue=True)
        cmd_proposal_create(args)

        check_sess = SessionFactory()
        proposal = check_sess.query(Proposal).filter(Proposal.business_id == biz.id).one()
        assert proposal.status == ProposalStatus.ISSUED.value
        assert float(proposal.total_price) == 14500.00
        check_sess.close()
