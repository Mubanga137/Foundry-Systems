"""
FS-CAE Conversations Inbox Router

Provides two-column conversation cockpit: intent-classified thread list,
chronological transcripts, 1-click 3-Point Audit dispatch, and Grand Slam Proposal issuance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.conversation import Conversation, Message
from app.schemas.states import (
    CommercialAuditStatus,
    ConversationState,
    DeliveryStatus,
    MessageDirection,
)
from app.services.conversation.classifier import classify_inbound_intent
from app.services.conversation.conversation_service import ConversationService
from app.services.outreach.dispatcher import is_within_operating_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox")
templates = Jinja2Templates(directory="app/templates")


def _format_time_ago(dt: Optional[datetime]) -> str:
    """Format datetime into human-friendly relative string."""
    if not dt:
        return "recently"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


@router.get("", response_class=HTMLResponse)
def get_inbox(
    request: Request,
    conversation_id: Optional[str] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # Fetch all conversations ordered by recent activity
    raw_convs = (
        db.query(Conversation)
        .order_by(Conversation.last_activity_at.desc(), Conversation.created_at.desc())
        .all()
    )

    conversations_list = []
    selected_conv_obj = None

    for c in raw_convs:
        biz = c.business
        last_msg = (
            db.query(Message)
            .filter(Message.conversation_id == c.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        last_inbound = (
            db.query(Message)
            .filter(
                Message.conversation_id == c.id,
                Message.direction == MessageDirection.INBOUND.value,
            )
            .order_by(Message.created_at.desc())
            .first()
        )

        latest_intent = None
        if last_inbound:
            try:
                latest_intent = classify_inbound_intent(last_inbound.content).value
            except Exception:
                latest_intent = "GENERAL_QUESTION"

        conv_dict = {
            "id": c.id,
            "company_name": biz.company_name if biz else "Contractor",
            "location": biz.location_district if biz else "Zambia",
            "phone": biz.phone_normalized if biz else "",
            "channel": c.channel,
            "state": c.state,
            "latest_snippet": last_msg.content[:80] if last_msg else None,
            "latest_intent": latest_intent,
            "time_ago": _format_time_ago(c.last_activity_at or c.created_at),
        }
        conversations_list.append(conv_dict)

        if conversation_id and c.id == conversation_id:
            selected_conv_obj = c

    # Default to first conversation if not explicitly selected
    if selected_conv_obj is None and raw_convs:
        selected_conv_obj = raw_convs[0]

    messages_list = []
    selected_conv_view = None
    pending_audit = None

    if selected_conv_obj:
        b = selected_conv_obj.business
        selected_conv_view = {
            "id": selected_conv_obj.id,
            "business_id": b.id if b else "",
            "company_name": b.company_name if b else "Contractor",
            "location": b.location_district if b else "Zambia",
            "phone": b.phone_normalized if b else "",
            "channel": selected_conv_obj.channel,
            "state": selected_conv_obj.state,
        }

        # Check for pending audit
        if b:
            pending_audit = (
                db.query(CommercialAudit)
                .filter(
                    CommercialAudit.business_id == b.id,
                    CommercialAudit.status == CommercialAuditStatus.GENERATED.value,
                )
                .first()
            )

        # Fetch messages in chronological order
        msgs = (
            db.query(Message)
            .filter(Message.conversation_id == selected_conv_obj.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        for m in msgs:
            time_str = m.created_at.strftime("%b %d, %H:%M") if m.created_at else ""
            messages_list.append(
                {
                    "id": m.id,
                    "direction": m.direction,
                    "content": m.content,
                    "delivery_status": m.delivery_status,
                    "time_formatted": time_str,
                }
            )

    dispatch_active = is_within_operating_hours()

    return templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context={
            "active_page": "inbox",
            "conversations": conversations_list,
            "selected_conv": selected_conv_view,
            "messages": messages_list,
            "pending_audit": pending_audit is not None,
            "dispatch_active": dispatch_active,
            "message": message,
            "error": error,
        },
    )


@router.post("/{conversation_id}/audit/approve")
def approve_audit(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Operator approves 3-point capability audit and transitions state to AUDIT_DISPATCHED."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    service = ConversationService()
    try:
        result = service.approve_and_send_audit(db, conversation_id)
        delivered_at_str = result["delivered_at"].isoformat() if result.get("delivered_at") else None
        serialized_data = {**result, "delivered_at": delivered_at_str}
        msg = "3-Point Commercial Capability Audit successfully approved and dispatched."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "SUCCESS", "message": msg, "data": serialized_data})
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&message={msg}",
            status_code=303,
        )
    except Exception as e:
        logger.error("Audit approval failed: %s", e)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "ERROR", "error": str(e)}, status_code=400)
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&error={str(e)}",
            status_code=303,
        )


@router.post("/{conversation_id}/proposal/issue")
def issue_grand_slam_proposal(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Operator issues K14,500 Grand Slam Proposal with 38/31/31 terms."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    service = ConversationService()
    try:
        proposal = service.issue_proposal_for_conversation(db, conversation_id)
        msg = f"Grand Slam Proposal {proposal.id} successfully issued to prospect."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "status": "SUCCESS",
                "message": msg,
                "proposal_id": proposal.id,
                "total_price": float(proposal.total_price),
            })
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&message={msg}",
            status_code=303,
        )
    except Exception as e:
        logger.error("Proposal issuance failed: %s", e)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "ERROR", "error": str(e)}, status_code=400)
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&error={str(e)}",
            status_code=303,
        )


@router.post("/{conversation_id}/reply")
def reply_to_conversation(
    conversation_id: str,
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    """Operator manual response input to a prospect."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msg = Message(
        conversation_id=conv.id,
        direction=MessageDirection.OUTBOUND.value,
        content=content.strip(),
        delivery_status=DeliveryStatus.SENT.value,
        raw_metadata={"source": "operator_cockpit_manual_reply"},
    )
    db.add(msg)
    conv.last_activity_at = datetime.now(timezone.utc)
    db.commit()

    return RedirectResponse(
        url=f"/inbox?conversation_id={conversation_id}&message=Reply+sent+successfully",
        status_code=303,
    )


@router.post("/{conversation_id}/reply-inbound")
def simulate_inbound_reply(
    conversation_id: str,
    request: Request,
    content: str = Form(...),
    channel: str = Form("FB_MESSENGER"),
    db: Session = Depends(get_db),
):
    """Simulate or paste a real inbound reply from the prospect.

    Calls ConversationService.handle_inbound_message to run full intent
    classification and state machine transitions.
    """
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    service = ConversationService()
    try:
        result = service.handle_inbound_message(
            db, conv.business_id, content.strip(), channel
        )
        intent = result.get("intent", "UNKNOWN")
        new_state = result.get("new_state", conv.state)
        msg = f"Inbound reply simulated. Intent: {intent}, New state: {new_state}"

        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "SUCCESS", "message": msg, "data": result})
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&message={msg}",
            status_code=303,
        )
    except Exception as e:
        logger.error("Inbound simulation failed: %s", e)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "ERROR", "error": str(e)}, status_code=400)
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&error={str(e)}",
            status_code=303,
        )


@router.post("/{conversation_id}/proposal/accept-and-invoice")
def accept_proposal_and_invoice(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Accept a proposal and generate K5,500 Mobilization Invoice.

    Transitions Proposal to ACCEPTED, creates a MOBILIZATION milestone invoice,
    and creates a ProjectActivation in PENDING_MOBILIZATION.
    """
    from app.models.commercial import Proposal
    from app.schemas.states import MilestoneTier, ProposalStatus
    from app.services.commercial.invoice_service import create_milestone_invoice

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Find the most recent ISSUED proposal for this business
    proposal = (
        db.query(Proposal)
        .filter(
            Proposal.business_id == conv.business_id,
            Proposal.status.in_([
                ProposalStatus.ISSUED.value,
                ProposalStatus.ACCEPTED.value,
            ]),
        )
        .order_by(Proposal.created_at.desc())
        .first()
    )

    if proposal is None:
        err = "No issued proposal found for this prospect. Issue a proposal first."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "ERROR", "error": err}, status_code=400)
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&error={err}",
            status_code=303,
        )

    try:
        invoice = create_milestone_invoice(
            db, proposal.id, milestone_tier=MilestoneTier.MOBILIZATION.value
        )
        msg = (
            f"Proposal accepted! K5,500 Mobilization Invoice {invoice.invoice_number} "
            f"generated and ready for reconciliation in /invoices."
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "status": "SUCCESS",
                "message": msg,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "amount": float(invoice.amount),
            })
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&message={msg}",
            status_code=303,
        )
    except Exception as e:
        logger.error("Accept-and-invoice failed: %s", e)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"status": "ERROR", "error": str(e)}, status_code=400)
        return RedirectResponse(
            url=f"/inbox?conversation_id={conversation_id}&error={str(e)}",
            status_code=303,
        )
