"""
FS-CAE Milestone Invoice Service

Generates sequential milestone invoices with strict config-driven payment instructions.
Ensures no payment details are fabricated. Sets up ProjectActivation in PENDING_MOBILIZATION
and transitions conversation to INVOICED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.exceptions import InvalidStateTransitionError
from app.models.business import Business
from app.models.commercial import Invoice, ProjectActivation, Proposal
from app.models.conversation import Conversation
from app.schemas.states import (
    ConversationState,
    InvoiceStatus,
    MilestoneTier,
    ProjectActivationStatus,
    ProposalStatus,
)
from app.services.state_machine import (
    transition_conversation,
    transition_invoice,
    transition_proposal,
)

logger = logging.getLogger(__name__)


def generate_payment_instructions() -> str:
    """
    Generate payment instruction text strictly from verified settings.
    If bank configuration is missing, returns explicit pending notice without inventing fake details.
    """
    settings = get_settings()

    if not settings.FOUNDRY_BANK_NAME or not settings.FOUNDRY_ACCOUNT_NUMBER:
        return "[PAYMENT INSTRUCTIONS PENDING OPERATOR CONFIGURATION]"

    branch_code = settings.FOUNDRY_BRANCH_CODE or "N/A"
    return (
        f"BANK TRANSFER DETAILS:\n"
        f"Entity: {settings.FOUNDRY_LEGAL_NAME}\n"
        f"Bank: {settings.FOUNDRY_BANK_NAME}\n"
        f"Account No: {settings.FOUNDRY_ACCOUNT_NUMBER}\n"
        f"Branch Code: {branch_code}"
    )


def generate_sequential_invoice_number(db: Session) -> str:
    """Generate unique deterministic invoice number INV-2026-XXXX."""
    current_year = 2026
    count = db.query(Invoice).count() + 1
    while True:
        candidate = f"INV-{current_year}-{count:04d}"
        exists = db.query(Invoice).filter(Invoice.invoice_number == candidate).first()
        if not exists:
            return candidate
        count += 1


def format_invoice_markdown(invoice: Invoice, proposal: Proposal, business: Business) -> str:
    """Format formal milestone invoice into structured Markdown presentation."""
    tier_label = {
        MilestoneTier.MOBILIZATION.value: "Milestone 1 — Mobilization & Engineering Setup (38%)",
        MilestoneTier.MID_PROJECT.value: "Milestone 2 — Mid-Project Alpha Review (31%)",
        MilestoneTier.PRE_LAUNCH.value: "Milestone 3 — Final Acceptance & Live Deployment (31%)",
    }.get(invoice.milestone_tier, invoice.milestone_tier)

    return (
        f"# COMMERCIAL TAX INVOICE\n\n"
        f"**Invoice Number:** {invoice.invoice_number}\n"
        f"**Date of Issue:** {invoice.created_at.strftime('%B %d, %Y')}\n"
        f"**Due Date:** {invoice.due_date.strftime('%B %d, %Y')}\n"
        f"**Status:** {invoice.status}\n\n"
        f"---\n\n"
        f"### ISSUER:\n"
        f"**Foundry Systems (Elysium Group Limited)**\n"
        f"Enterprise Systems & Capability Engineering Practice\n"
        f"Lusaka, Zambia\n\n"
        f"### BILLED TO:\n"
        f"**{business.company_name}**\n"
        f"Location: {business.location_district or 'Zambia'}\n"
        f"Attn: Managing Director / Operations Head\n\n"
        f"---\n\n"
        f"### LINE ITEM DESCRIPTION:\n"
        f"| Description | Milestone Tier | Amount (ZMW) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| Capability Portal Core Engineering | {tier_label} | K{float(invoice.amount):,.2f} |\n\n"
        f"**TOTAL DUE:** **K{float(invoice.amount):,.2f} ZMW**\n\n"
        f"---\n\n"
        f"### PAYMENT INSTRUCTIONS:\n"
        f"{invoice.payment_instructions or '[PAYMENT INSTRUCTIONS PENDING OPERATOR CONFIGURATION]'}\n"
        f"**Reference:** Quote `{invoice.invoice_number}` upon transfer.\n"
    )


def create_milestone_invoice(
    db: Session,
    proposal_id: str,
    milestone_tier: str = MilestoneTier.MOBILIZATION.value,
) -> Invoice:
    """
    Create a milestone invoice for a proposal:
    1. Validates proposal is in ISSUED or ACCEPTED state (transitions ISSUED -> ACCEPTED).
    2. Pulls exact milestone amount.
    3. Generates unique sequential number INV-2026-XXXX.
    4. Attaches verified payment instructions or pending notice.
    5. Transitions invoice DRAFT -> ISSUED.
    6. Creates/updates ProjectActivation in PENDING_MOBILIZATION if tier is MOBILIZATION.
    7. Transitions Conversation to INVOICED.
    """
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).one()
    business = proposal.business

    # 1. State check on proposal
    if proposal.status == ProposalStatus.DRAFT.value:
        raise InvalidStateTransitionError(
            "Proposal", proposal.status, ProposalStatus.ACCEPTED.value,
            reason="Proposal must be ISSUED before milestone invoice can be created",
        )
    elif proposal.status == ProposalStatus.ISSUED.value:
        transition_proposal(
            db,
            proposal,
            ProposalStatus.ACCEPTED.value,
            actor="COMMERCIAL_SERVICE",
            reason="Proposal accepted for milestone invoice generation",
        )

    # 2. Amount selection
    tier = milestone_tier.upper()
    if tier == MilestoneTier.MOBILIZATION.value:
        amount = float(proposal.milestone_1_mobilization)
    elif tier == MilestoneTier.MID_PROJECT.value:
        amount = float(proposal.milestone_2_midway)
    elif tier == MilestoneTier.PRE_LAUNCH.value:
        amount = float(proposal.milestone_3_launch)
    else:
        raise ValueError(f"Unknown milestone tier: '{milestone_tier}'")

    # 3. Invoice Number & Payment Instructions
    inv_number = generate_sequential_invoice_number(db)
    instructions = generate_payment_instructions()
    due_date = datetime.now(timezone.utc) + timedelta(days=7)

    # 4. Create Invoice
    invoice = Invoice(
        proposal_id=proposal.id,
        invoice_number=inv_number,
        milestone_tier=tier,
        amount=amount,
        status=InvoiceStatus.DRAFT.value,
        payment_instructions=instructions,
        due_date=due_date,
    )
    db.add(invoice)
    db.flush()

    # Transition invoice DRAFT -> ISSUED
    transition_invoice(
        db,
        invoice,
        InvoiceStatus.ISSUED.value,
        actor="INVOICE_SERVICE",
        reason=f"Issued {tier} invoice {inv_number}",
    )

    # 5. ProjectActivation initialization on Mobilization
    if tier == MilestoneTier.MOBILIZATION.value:
        activation = (
            db.query(ProjectActivation)
            .filter(ProjectActivation.business_id == business.id)
            .first()
        )
        if activation is None:
            activation = ProjectActivation(
                business_id=business.id,
                proposal_id=proposal.id,
                mobilization_invoice_id=invoice.id,
                status=ProjectActivationStatus.PENDING_MOBILIZATION.value,
            )
            db.add(activation)
            db.flush()
        else:
            activation.mobilization_invoice_id = invoice.id
            activation.status = ProjectActivationStatus.PENDING_MOBILIZATION.value

    # 6. Conversation state transition to INVOICED
    conv = (
        db.query(Conversation)
        .filter(Conversation.business_id == business.id)
        .first()
    )
    if conv and conv.state != ConversationState.INVOICED.value:
        transition_conversation(
            db,
            conv,
            ConversationState.INVOICED.value,
            actor="INVOICE_SERVICE",
            reason=f"Mobilization invoice {inv_number} issued to prospect",
        )

    db.commit()
    db.refresh(invoice)
    return invoice
