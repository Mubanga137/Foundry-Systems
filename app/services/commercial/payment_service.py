"""
FS-CAE Payment Reconciliation Service

Enforces the non-negotiable financial integrity gate:
- Verified transaction references required before marking invoices PAID.
- Rejects duplicate transaction references.
- Validates payment amount meets or exceeds invoice total.
- Automatically triggers client activation on mobilization invoice reconciliation.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.exceptions import DuplicatePaymentError
from app.models.commercial import Invoice, PaymentEvent
from app.schemas.states import MilestoneTier
from app.services.commercial.activation_service import activate_client_project
from app.services.state_machine import record_payment

logger = logging.getLogger(__name__)


def reconcile_payment(
    db: Session,
    invoice_id: str,
    amount_received: float,
    transaction_reference: str,
    verified_by: str,
    notes: Optional[str] = None,
) -> PaymentEvent:
    """
    Reconcile an incoming payment against an issued invoice:
    1. Validates amount_received >= invoice.amount.
    2. Enforces unique transaction_reference across all payment events.
    3. Records immutable PaymentEvent and transitions Invoice to PAID.
    4. Triggers client project activation if this is the mobilization invoice.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).one()

    # 1. Check for duplicate reference
    existing_event = (
        db.query(PaymentEvent)
        .filter(PaymentEvent.transaction_reference == transaction_reference.strip())
        .first()
    )
    if existing_event:
        logger.error(
            "Duplicate payment reference detected: %s (already reconciled for invoice %s)",
            transaction_reference,
            existing_event.invoice_id,
        )
        raise DuplicatePaymentError(transaction_reference.strip())

    # 2. Check sufficient payment amount
    if float(amount_received) < float(invoice.amount):
        logger.error(
            "Insufficient payment amount: received K%s, required K%s for invoice %s",
            amount_received,
            invoice.amount,
            invoice.invoice_number,
        )
        raise ValueError(
            f"Insufficient payment amount K{amount_received:,.2f} for invoice {invoice.invoice_number} "
            f"(required: K{float(invoice.amount):,.2f})"
        )

    # 3. Record payment via StateMachineService
    event = record_payment(
        db,
        invoice,
        amount_received=float(amount_received),
        transaction_reference=transaction_reference.strip(),
        verified_by=verified_by.strip(),
        notes=notes,
        actor=verified_by.strip(),
    )

    logger.info(
        "Successfully reconciled payment K%s for invoice %s (ref: %s)",
        amount_received,
        invoice.invoice_number,
        transaction_reference,
    )

    # 4. Trigger mobilization activation
    if invoice.milestone_tier == MilestoneTier.MOBILIZATION.value:
        logger.info(
            "Mobilization invoice %s paid. Activating client project.",
            invoice.invoice_number,
        )
        activate_client_project(db, invoice.id)

    db.commit()
    db.refresh(event)
    return event


class PaymentReconciliationService:
    """Class wrapper for payment reconciliation."""

    def __init__(self, db: Session):
        self.db = db

    def reconcile(
        self,
        invoice_id: str,
        amount_received: float,
        transaction_reference: str,
        verified_by: str,
        notes: Optional[str] = None,
    ) -> PaymentEvent:
        return reconcile_payment(
            self.db,
            invoice_id=invoice_id,
            amount_received=amount_received,
            transaction_reference=transaction_reference,
            verified_by=verified_by,
            notes=notes,
        )
