"""
FS-CAE Financial Reconciliation Desk Router

Manages milestone invoice ledger, payment reconciliation against bank references,
and automatic client project activation upon mobilization fee verification.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import DuplicatePaymentError, UnmetMilestoneError
from app.models.business import Business
from app.models.commercial import Invoice, PaymentEvent, ProjectActivation
from app.schemas.states import (
    InvoiceStatus,
    MilestoneTier,
    ProjectActivationStatus,
)
from app.services.commercial.payment_service import reconcile_payment
from app.services.outreach.dispatcher import is_within_operating_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices")
templates = Jinja2Templates(directory="app/templates")


def _get_invoices_context(db: Session) -> Dict[str, Any]:
    """Helper to assemble financial desk metrics and invoice ledger."""
    invoices_records = (
        db.query(Invoice)
        .order_by(Invoice.created_at.desc())
        .all()
    )

    invoices_list = []
    total_invoiced = 0.0
    total_paid = 0.0
    pending_mobilization_count = 0

    for inv in invoices_records:
        biz = inv.proposal.business if (inv.proposal and inv.proposal.business) else None
        amt = float(inv.amount)
        total_invoiced += amt
        if inv.status == InvoiceStatus.PAID.value:
            total_paid += amt
        elif (
            inv.status == InvoiceStatus.ISSUED.value
            and inv.milestone_tier == MilestoneTier.MOBILIZATION.value
        ):
            pending_mobilization_count += 1

        due_str = inv.due_date.strftime("%b %d, %Y") if inv.due_date else "N/A"

        invoices_list.append(
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "client_name": biz.company_name if biz else "Contractor",
                "milestone_tier": inv.milestone_tier,
                "amount": amt,
                "due_date_str": due_str,
                "status": inv.status,
            }
        )

    activated_count = (
        db.query(func.count(ProjectActivation.id))
        .filter(ProjectActivation.status == ProjectActivationStatus.ACTIVATED.value)
        .scalar()
        or 0
    )

    return {
        "invoices": invoices_list,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "pending_mobilization_count": pending_mobilization_count,
        "activated_count": activated_count,
    }


@router.get("", response_class=HTMLResponse)
def get_invoices(
    request: Request,
    message: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    ctx = _get_invoices_context(db)
    dispatch_active = is_within_operating_hours()

    return templates.TemplateResponse(
        request=request,
        name="invoices.html",
        context={
            "active_page": "invoices",
            "dispatch_active": dispatch_active,
            "message": message,
            "error": error,
            **ctx,
        },
    )


@router.post("/reconcile")
def reconcile_invoice_payment(
    request: Request,
    invoice_id: str = Form(...),
    amount_received: float = Form(...),
    transaction_reference: str = Form(...),
    verified_by: str = Form("Founder"),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Reconcile bank payment against issued invoice.
    Enforces non-negotiable payment integrity gate:
      - Validates amount_received >= invoice.amount
      - Rejects duplicate transaction references (HTTP 400)
      - Automatically triggers project activation if mobilization tier
    """
    is_json = "application/json" in request.headers.get("accept", "") or "application/json" in request.headers.get("content-type", "")

    try:
        event = reconcile_payment(
            db=db,
            invoice_id=invoice_id,
            amount_received=float(amount_received),
            transaction_reference=transaction_reference.strip(),
            verified_by=verified_by.strip(),
            notes=notes,
        )

        msg = f"Payment of K{amount_received:,.2f} reconciled via ref {transaction_reference}. Client activated!"
        if is_json:
            return JSONResponse({
                "status": "PAID",
                "payment_event_id": event.id,
                "transaction_reference": event.transaction_reference,
                "message": msg,
            })

        return RedirectResponse(
            url=f"/invoices?message={msg}",
            status_code=303,
        )

    except DuplicatePaymentError as e:
        logger.warning("Duplicate payment reference rejected: %s", e)
        error_msg = f"Duplicate payment reference: '{transaction_reference}' has already been reconciled."
        if is_json:
            return JSONResponse({"status": "DUPLICATE", "error": error_msg}, status_code=400)

        ctx = _get_invoices_context(db)
        return templates.TemplateResponse(
            request=request,
            name="invoices.html",
            context={
                "active_page": "invoices",
                "dispatch_active": is_within_operating_hours(),
                "error": error_msg,
                **ctx,
            },
            status_code=400,
        )

    except (ValueError, UnmetMilestoneError) as e:
        logger.warning("Payment validation failed: %s", e)
        error_msg = str(e)
        if is_json:
            return JSONResponse({"status": "ERROR", "error": error_msg}, status_code=400)

        ctx = _get_invoices_context(db)
        return templates.TemplateResponse(
            request=request,
            name="invoices.html",
            context={
                "active_page": "invoices",
                "dispatch_active": is_within_operating_hours(),
                "error": error_msg,
                **ctx,
            },
            status_code=400,
        )
