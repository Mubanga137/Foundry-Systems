"""
FS-CAE Conversation Coordinator & State Service

Coordinates inbound messages, runs intent classification, drives state transitions
through Layer 1's StateMachineService, manages prospect suppression on opt-out,
and automates 3-point capability audit generation and proposal issuance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.commercial import Proposal
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.schemas.states import (
    CommercialAuditStatus,
    ConversationChannel,
    ConversationState,
    DeliveryStatus,
    DispatchStatus,
    InboundIntent,
    MessageDirection,
    PackageType,
    ProposalStatus,
)
from app.services.commercial.audit_generator import (
    format_audit_markdown,
    generate_commercial_audit,
)
from app.services.commercial.proposal_generator import (
    create_grand_slam_proposal,
    format_proposal_markdown,
    issue_proposal,
)
from app.services.conversation.classifier import classify_inbound_intent
from app.services.conversation.response_generator import (
    generate_call_or_meeting_response,
    generate_existing_website_response,
    generate_general_question_response,
    generate_pricing_response,
    generate_timing_delay_response,
)
from app.services.state_machine import (
    _write_audit_log,
    transition_audit,
    transition_conversation,
    transition_dispatch,
)

logger = logging.getLogger(__name__)


class ConversationService:
    """Manages conversations, message ingestion, and automated sales workflows."""

    def __init__(self, gemini_client: Optional[Any] = None):
        self.gemini_client = gemini_client

    def get_or_create_conversation(
        self,
        db: Session,
        business_id: str,
        channel: str = ConversationChannel.FB_MESSENGER.value,
    ) -> Conversation:
        """Find existing conversation or create a new one in AWAITING_REPLY."""
        conv = (
            db.query(Conversation)
            .filter(Conversation.business_id == business_id)
            .first()
        )
        if conv is None:
            conv = Conversation(
                business_id=business_id,
                channel=channel,
                state=ConversationState.AWAITING_REPLY.value,
            )
            db.add(conv)
            db.flush()
            _write_audit_log(
                db,
                entity_type="Conversation",
                entity_id=conv.id,
                old_state=None,
                new_state=ConversationState.AWAITING_REPLY.value,
                actor="SYSTEM",
                reason="Initialized new conversation",
            )
            db.commit()
        return conv

    def handle_inbound_message(
        self,
        db: Session,
        business_id: str,
        text: str,
        channel: str = ConversationChannel.FB_MESSENGER.value,
    ) -> Dict[str, Any]:
        """
        Ingest an incoming message, classify intent, update state machine,
        handle suppression on opt-out, and generate automated drafts.
        """
        business = db.query(Business).filter(Business.id == business_id).one()
        conv = self.get_or_create_conversation(db, business_id, channel)

        # 1. Record inbound message
        inbound_msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND.value,
            content=text,
            delivery_status=DeliveryStatus.RECEIVED.value,
            raw_metadata={"channel": channel, "source": "inbound_webhook"},
        )
        db.add(inbound_msg)
        conv.last_activity_at = datetime.now(timezone.utc)
        db.commit()

        # Auto-cancel any pending follow-up jobs now that prospect responded
        from app.services.outreach.followup_service import cancel_pending_followups
        cancel_pending_followups(db, business_id, reason="Prospect responded to outreach")

        # 2. Intent Classification
        intent = classify_inbound_intent(text, self.gemini_client)
        logger.info(
            "Classified inbound message for business %s (%s) as: %s",
            business.id,
            business.company_name,
            intent.value,
        )

        result_payload: Dict[str, Any] = {
            "conversation_id": conv.id,
            "business_id": business.id,
            "intent": intent.value,
            "old_state": conv.state,
            "new_state": conv.state,
            "suggested_reply": None,
            "audit_id": None,
            "proposal_id": None,
        }

        # 3. State Machine Transitions based on Intent
        if intent == InboundIntent.OPT_OUT:
            # Transition Conversation to OPTED_OUT
            transition_conversation(
                db,
                conv,
                ConversationState.OPTED_OUT.value,
                actor="PROSPECT",
                reason="Prospect requested opt-out / unsubscribed",
            )

            # Suppress the business entity
            business.is_suppressed = True
            business.suppressed_reason = "Prospect requested opt-out"

            # Cancel / Block any active or queued dispatch jobs
            pending_jobs = (
                db.query(DispatchJob)
                .filter(
                    DispatchJob.business_id == business.id,
                    DispatchJob.status.in_([
                        DispatchStatus.DRAFTED.value,
                        DispatchStatus.QUEUED.value,
                        DispatchStatus.SENDING.value,
                    ]),
                )
                .all()
            )
            for j in pending_jobs:
                transition_dispatch(
                    db,
                    j,
                    DispatchStatus.BLOCKED.value,
                    blocked_reason="Prospect opted out of communications",
                    actor="CONVERSATION_SERVICE",
                    reason="Aborted job due to prospect opt-out",
                )

            db.commit()
            result_payload["new_state"] = ConversationState.OPTED_OUT.value
            result_payload["suggested_reply"] = None
            return result_payload

        elif intent == InboundIntent.POSITIVE_INTEREST:
            # Move from AWAITING_REPLY -> POSITIVE_REPLY -> AUDIT_PENDING_APPROVAL
            if conv.state == ConversationState.AWAITING_REPLY.value:
                transition_conversation(
                    db,
                    conv,
                    ConversationState.POSITIVE_REPLY.value,
                    actor="PROSPECT",
                    reason="Prospect indicated positive interest",
                )

            # Generate the 3-point capability audit
            audit = generate_commercial_audit(db, business.id)

            # Transition to AUDIT_PENDING_APPROVAL
            transition_conversation(
                db,
                conv,
                ConversationState.AUDIT_PENDING_APPROVAL.value,
                actor="CONVERSATION_SERVICE",
                reason=f"Generated audit {audit.id}; awaiting operator approval",
            )
            db.commit()

            result_payload["new_state"] = ConversationState.AUDIT_PENDING_APPROVAL.value
            result_payload["audit_id"] = audit.id
            result_payload["suggested_reply"] = format_audit_markdown(audit, business)
            return result_payload

        elif intent == InboundIntent.REQUEST_PRICING:
            # Move to OFFER_PENDING_APPROVAL
            transition_conversation(
                db,
                conv,
                ConversationState.OFFER_PENDING_APPROVAL.value,
                actor="PROSPECT",
                reason="Prospect requested pricing/quotation",
            )
            db.commit()

            reply = generate_pricing_response(business)
            result_payload["new_state"] = ConversationState.OFFER_PENDING_APPROVAL.value
            result_payload["suggested_reply"] = reply
            return result_payload

        elif intent == InboundIntent.HAS_EXISTING_WEBSITE:
            # Move to OBJECTION_OR_QUESTION
            transition_conversation(
                db,
                conv,
                ConversationState.OBJECTION_OR_QUESTION.value,
                actor="PROSPECT",
                reason="Prospect noted they already have a website",
            )
            db.commit()

            reply = generate_existing_website_response(business)
            result_payload["new_state"] = ConversationState.OBJECTION_OR_QUESTION.value
            result_payload["suggested_reply"] = reply
            return result_payload

        elif intent == InboundIntent.CALL_OR_MEETING_REQUEST:
            transition_conversation(
                db,
                conv,
                ConversationState.OBJECTION_OR_QUESTION.value,
                actor="PROSPECT",
                reason="Prospect requested phone call or meeting",
            )
            db.commit()

            reply = generate_call_or_meeting_response(business)
            result_payload["new_state"] = ConversationState.OBJECTION_OR_QUESTION.value
            result_payload["suggested_reply"] = reply
            return result_payload

        elif intent == InboundIntent.TIMING_DELAY:
            transition_conversation(
                db,
                conv,
                ConversationState.OBJECTION_OR_QUESTION.value,
                actor="PROSPECT",
                reason="Prospect requested timing delay",
            )
            db.commit()

            reply = generate_timing_delay_response(business)
            result_payload["new_state"] = ConversationState.OBJECTION_OR_QUESTION.value
            result_payload["suggested_reply"] = reply
            return result_payload

        else:  # GENERAL_QUESTION
            transition_conversation(
                db,
                conv,
                ConversationState.OBJECTION_OR_QUESTION.value,
                actor="PROSPECT",
                reason="Prospect posed general inquiry",
            )
            db.commit()

            reply = generate_general_question_response(business, text)
            result_payload["new_state"] = ConversationState.OBJECTION_OR_QUESTION.value
            result_payload["suggested_reply"] = reply
            return result_payload

    def approve_and_send_audit(
        self,
        db: Session,
        conversation_id: str,
    ) -> Dict[str, Any]:
        """
        Operator approval step: moves conversation from AUDIT_PENDING_APPROVAL
        to AUDIT_DISPATCHED, approves the audit, and records the outbound message.
        """
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).one()
        business = conv.business

        audit = (
            db.query(CommercialAudit)
            .filter(CommercialAudit.business_id == business.id)
            .order_by(CommercialAudit.created_at.desc())
            .first()
        )
        if audit is None:
            raise ValueError(f"No CommercialAudit found for business {business.id}")

        # Transition audit status GENERATED -> APPROVED -> DELIVERED
        if audit.status == CommercialAuditStatus.GENERATED.value:
            transition_audit(
                db,
                audit,
                CommercialAuditStatus.APPROVED.value,
                actor="OPERATOR",
                reason="Approved for customer delivery",
            )

        transition_audit(
            db,
            audit,
            CommercialAuditStatus.DELIVERED.value,
            actor="OPERATOR",
            reason="Commercial audit delivered to prospect",
        )

        # Transition conversation state to AUDIT_DISPATCHED
        transition_conversation(
            db,
            conv,
            ConversationState.AUDIT_DISPATCHED.value,
            actor="OPERATOR",
            reason="3-Point Capability Audit dispatched to prospect",
        )

        # Record outbound message
        audit_content = format_audit_markdown(audit, business)
        outbound_msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.OUTBOUND.value,
            content=audit_content,
            delivery_status=DeliveryStatus.SENT.value,
            raw_metadata={"audit_id": audit.id},
        )
        db.add(outbound_msg)
        db.commit()

        return {
            "conversation_id": conv.id,
            "audit_id": audit.id,
            "status": conv.state,
            "delivered_at": audit.delivered_at,
        }

    def issue_proposal_for_conversation(
        self,
        db: Session,
        conversation_id: str,
        package_type: str = PackageType.CAPABILITY_PORTAL_CORE.value,
    ) -> Proposal:
        """
        Construct, issue, and log a Grand Slam proposal for a conversation,
        transitioning the conversation to PROPOSAL_REQUESTED.
        """
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).one()
        business = conv.business

        # 1. Create proposal in DRAFT
        proposal = create_grand_slam_proposal(db, business.id, package_type)

        # 2. Issue proposal
        proposal = issue_proposal(db, proposal.id)

        # 3. Transition conversation to PROPOSAL_REQUESTED
        transition_conversation(
            db,
            conv,
            ConversationState.PROPOSAL_REQUESTED.value,
            actor="COMMERCIAL_SERVICE",
            reason=f"Grand Slam Proposal {proposal.id} issued",
        )

        # 4. Record outbound message
        proposal_text = format_proposal_markdown(proposal, business)
        outbound_msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.OUTBOUND.value,
            content=proposal_text,
            delivery_status=DeliveryStatus.SENT.value,
            raw_metadata={"proposal_id": proposal.id, "package_type": package_type},
        )
        db.add(outbound_msg)
        db.commit()
        return proposal
