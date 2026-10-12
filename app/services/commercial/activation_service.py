"""
FS-CAE Client Activation & Builder.io Delivery Brief Service

Activates commercial contractor projects upon verified mobilization payment,
transitions conversation state to WON, and compiles comprehensive implementation
delivery briefs featuring the Builder.io architectural master prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.exceptions import UnmetMilestoneError
from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.commercial import Invoice, ProjectActivation, Proposal
from app.models.conversation import Conversation
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    ConversationState,
    InvoiceStatus,
    ProjectActivationStatus,
)
from app.services.state_machine import activate_project, transition_conversation

logger = logging.getLogger(__name__)


def compile_delivery_brief(db: Session, business_id: str) -> Dict[str, Any]:
    """
    Compile comprehensive implementation delivery brief for an activated client.
    Aggregates business intelligence, detected assets, audit findings, and produces
    the Builder.io Master Generation Prompt.
    """
    business = db.query(Business).filter(Business.id == business_id).one()
    qual = business.qualification
    audit = (
        db.query(CommercialAudit)
        .filter(CommercialAudit.business_id == business.id)
        .order_by(CommercialAudit.created_at.desc())
        .first()
    )
    proposal = (
        db.query(Proposal)
        .filter(Proposal.business_id == business.id)
        .order_by(Proposal.created_at.desc())
        .first()
    )

    # Format asset footprint summary
    assets_summary = "Commercial plant and industrial fleet"
    if qual and qual.asset_footprint:
        if isinstance(qual.asset_footprint, dict):
            items = []
            for v in qual.asset_footprint.values():
                if isinstance(v, list):
                    items.extend([str(x) for x in v])
                elif isinstance(v, str):
                    items.append(v)
            if items:
                assets_summary = ", ".join(items[:5])
        elif isinstance(qual.asset_footprint, list):
            assets_summary = ", ".join([str(x) for x in qual.asset_footprint[:5]])
        elif isinstance(qual.asset_footprint, str):
            assets_summary = qual.asset_footprint

    # Audit findings
    findings = []
    if audit:
        findings = [
            audit.finding_1 or "Machinery specs unlisted",
            audit.finding_2 or "Lack of mobile RFQ capture",
            audit.finding_3 or "Compliance credentials unverified",
        ]

    # Construct Builder.io Master Prompt
    builder_prompt = (
        f"Generate a high-converting, mobile-first B2B commercial capability portal for '{business.company_name}', "
        f"an industrial contractor operating along {business.location_district or 'Lusaka, Zambia'}.\n\n"
        f"Primary Operational Assets:\n{assets_summary}\n\n"
        f"Design & Conversion Architecture:\n"
        f"1. Hero Section: Heavy industrial aesthetic (dark steel slate #0F172A, high-visibility safety amber #F59E0B accents). "
        f"Prominent verified operational corridor tag, WhatsApp RFQ quick-launch button, and direct mobile dispatch hotline.\n"
        f"2. Interactive Fleet Specification Matrix: Searchable by machine tonnage, bucket capacity, and mobilization terms. "
        f"Cards include high-contrast specification badges and 1-click 'Request Hire / RFQ' trigger.\n"
        f"3. Commercial Procurement Verification Badge: Verified PACRA, NCC registration, and EIZ compliance credential display.\n"
        f"4. 3-Step Instant RFQ Submission Flow: Mobile-optimized form for estimators with machinery selection, site delivery coordinates, and material volume requirements.\n"
        f"5. Statutory & Trust Bar: Corporate registration details, safety standards, and project delivery track record.\n"
        f"Style: Ultra-clean, direct, modern industrial executive B2B theme. Zero placeholder text."
    )

    brief_data = {
        "client_name": business.company_name,
        "business_id": business.id,
        "location": business.location_district or "Zambia",
        "phone": business.phone_normalized or business.phone_raw,
        "email": business.email,
        "package": proposal.package_type if proposal else "CAPABILITY_PORTAL_CORE",
        "total_value": float(proposal.total_price) if proposal else 14500.0,
        "mobilization_paid": True,
        "verified_assets": assets_summary,
        "audit_findings": findings,
        "builder_master_prompt": builder_prompt,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
    }
    return brief_data


def format_delivery_brief_markdown(brief: Dict[str, Any]) -> str:
    """Format delivery brief into structured Markdown presentation."""
    findings_str = "\n".join([f"- {f}" for f in brief.get("audit_findings", [])])

    return (
        f"# CLIENT IMPLEMENTATION DELIVERY BRIEF\n"
        f"**Client:** {brief['client_name']}\n"
        f"**Business ID:** `{brief['business_id']}`\n"
        f"**Territory:** {brief['location']}\n"
        f"**Package:** {brief['package']} (K{brief['total_value']:,.2f})\n"
        f"**Status:** ACTIVATED (Mobilization Payment Verified)\n\n"
        f"---\n\n"
        f"## 1. CLIENT OPERATIONAL PROFILE\n"
        f"- **Primary Assets:** {brief['verified_assets']}\n"
        f"- **Direct Phone:** {brief.get('phone') or 'N/A'}\n"
        f"- **Email:** {brief.get('email') or 'N/A'}\n\n"
        f"## 2. RESOLVED COMMERCIAL AUDIT GAPS\n"
        f"{findings_str}\n\n"
        f"---\n\n"
        f"## 3. BUILDER.IO MASTER ARCHITECTURAL PROMPT\n"
        f"```text\n"
        f"{brief['builder_master_prompt']}\n"
        f"```\n"
    )


def activate_client_project(
    db: Session,
    mobilization_invoice_id: str,
) -> ProjectActivation:
    """
    Activate a client project upon verified mobilization payment:
    1. Verifies mobilization invoice is PAID.
    2. Transitions ProjectActivation PENDING_MOBILIZATION -> ACTIVATED.
    3. Transitions Conversation to WON.
    """
    invoice = db.query(Invoice).filter(Invoice.id == mobilization_invoice_id).one()
    if invoice.status != InvoiceStatus.PAID.value:
        raise UnmetMilestoneError(
            f"Mobilization invoice {invoice.invoice_number} is in state '{invoice.status}'. "
            f"Must be strictly PAID before project activation."
        )

    # Find activation record
    activation = (
        db.query(ProjectActivation)
        .filter(ProjectActivation.mobilization_invoice_id == mobilization_invoice_id)
        .first()
    )
    if activation is None:
        # Check by business
        proposal = invoice.proposal
        activation = (
            db.query(ProjectActivation)
            .filter(ProjectActivation.business_id == proposal.business_id)
            .first()
        )
        if activation is None:
            activation = ProjectActivation(
                business_id=proposal.business_id,
                proposal_id=proposal.id,
                mobilization_invoice_id=invoice.id,
                status=ProjectActivationStatus.PENDING_MOBILIZATION.value,
            )
            db.add(activation)
            db.flush()

    # Transition activation if not already activated
    if activation.status != ProjectActivationStatus.ACTIVATED.value:
        activate_project(
            db,
            activation,
            actor="ACTIVATION_SERVICE",
            reason=f"Verified payment of mobilization invoice {invoice.invoice_number}",
        )

    # Transition conversation to WON
    business = activation.business
    conv = (
        db.query(Conversation)
        .filter(Conversation.business_id == business.id)
        .first()
    )
    if conv and conv.state != ConversationState.WON:
        transition_conversation(
            db,
            conv,
            ConversationState.WON,
            actor="ACTIVATION_SERVICE",
            reason="Project mobilized and activated upon payment verification",
        )

    db.commit()
    db.refresh(activation)
    return activation


class ActivationService:
    """Class wrapper for client activation and brief compilation."""

    def __init__(self, db: Session):
        self.db = db

    def activate(self, mobilization_invoice_id: str) -> ProjectActivation:
        return activate_client_project(self.db, mobilization_invoice_id)

    def get_brief(self, business_id: str) -> Dict[str, Any]:
        return compile_delivery_brief(self.db, business_id)

    def format_brief(self, brief: Dict[str, Any]) -> str:
        return format_delivery_brief_markdown(brief)
