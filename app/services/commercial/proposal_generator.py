"""
FS-CAE Grand Slam Proposal Generator

Constructs high-value, price-anchored B2B proposals for Zambian industrial contractors.
Enforces the K14,500 total fee with the exact 38/31/31 milestone structure (K5,500 / K4,500 / K4,500)
and optional K1,500/month ongoing operations retainer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.business import Business
from app.models.commercial import Proposal
from app.schemas.states import PackageType, ProposalStatus
from app.services.state_machine import transition_proposal

logger = logging.getLogger(__name__)

# Core Package Pricing Constants
CORE_TOTAL_PRICE = 14500.00
CORE_MILESTONE_1_MOBILIZATION = 5500.00  # ~38% upfront
CORE_MILESTONE_2_MIDWAY = 4500.00        # ~31% mid-point sign-off
CORE_MILESTONE_3_LAUNCH = 4500.00        # ~31% pre-launch handover
CORE_CONTINUITY_RETAINER = 1500.00       # K1,500/mo optional hosting/maintenance

# Basic Alternative Pricing Constants
BASIC_TOTAL_PRICE = 5500.00
BASIC_MILESTONE_1_MOBILIZATION = 5500.00
BASIC_MILESTONE_2_MIDWAY = 0.00
BASIC_MILESTONE_3_LAUNCH = 0.00
BASIC_CONTINUITY_RETAINER = 0.00


def format_proposal_markdown(proposal: Proposal, business: Business) -> str:
    """Format full structured Grand Slam proposal into professional Markdown."""
    pkg_name = (
        "Foundry Capability Portal (Core Enterprise Edition)"
        if proposal.package_type == PackageType.CAPABILITY_PORTAL_CORE.value
        else "Foundry Launch System (Basic Single-Page Edition)"
    )

    return (
        f"# COMMERCIAL PROPOSAL & SPECIFICATION\n\n"
        f"**Prepared For:** {business.company_name}\n"
        f"**Territory:** {business.location_district or 'Lusaka, Zambia'}\n"
        f"**Prepared By:** Foundry Systems (Elysium Group Limited)\n"
        f"**Date:** {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n"
        f"**Package:** {pkg_name}\n\n"
        f"---\n\n"
        f"## 1. STRATEGIC OBJECTIVE\n"
        f"To establish a verifiable, mobile-first commercial capability portal for {business.company_name}, "
        f"enabling procurement officers, tier-1 mining contractors, and civil engineers to audit plant tonnage, "
        f"inspect machinery specifications, and submit structured RFQs directly on mobile.\n\n"
        f"## 2. LINE-ITEMED VALUE BREAKDOWN\n"
        f"```text\n"
        f"SCOPE & VALUE BREAKDOWN:\n"
        f"• Enterprise UI/UX Architecture & Wireframing ........ K10,000 [INCLUDED]\n"
        f"• Fleet Spec Digitization & Asset Optimization ....... K8,500  [INCLUDED]\n"
        f"• Core Portal Engineering & Mobile RFQ Setup ......... K14,500\n"
        f"• Server Provisioning, Cloud Deployment & SSL ........ ~~K4,500~~ [WAIVED]\n"
        f"-----------------------------------------------------\n"
        f"TOTAL COMMERCIAL VALUE: .............................. K37,500\n"
        f"DISCOUNTED PORTFOLIO INVESTMENT: .................... K{proposal.total_price:,.2f}\n"
        f"```\n\n"
        f"## 3. MILESTONE INVESTMENT SCHEDULE (38 / 31 / 31)\n"
        f"To eliminate corporate delivery risk, commercial fees are distributed across three verified milestones:\n\n"
        f"1. **Milestone 1 — Mobilization & Engineering Setup:** **K{proposal.milestone_1_mobilization:,.2f}**\n"
        f"   Payable upfront before engineering mobilization. Covers domain provisioning, infrastructure architecture, "
        f"   and initial fleet asset compilation.\n\n"
        f"2. **Milestone 2 — Mid-Project Alpha Review:** **K{proposal.milestone_2_midway:,.2f}**\n"
        f"   Payable upon interactive UI layout sign-off and equipment catalogue approval.\n\n"
        f"3. **Milestone 3 — Final Acceptance & Live Deployment:** **K{proposal.milestone_3_launch:,.2f}**\n"
        f"   Payable prior to final DNS pointing, SSL cutover, and public production handover.\n\n"
        f"## 4. OPTIONAL OPERATIONS & HOSTING RETAINER\n"
        f"**K{proposal.continuity_monthly_fee:,.2f} / month** (Optional)\n"
        f"Includes managed cloud hosting, automated daily database backups, SSL security certificate renewal, "
        f"and quarterly fleet spec catalogue updates.\n\n"
        f"## 5. NEXT STEPS\n"
        f"Reply directly to approve Milestone 1 mobilization and receive the formal invoice.\n"
    )


def create_grand_slam_proposal(
    db: Session,
    business_id: str,
    package_type: str = PackageType.CAPABILITY_PORTAL_CORE.value,
) -> Proposal:
    """
    Create a formal Proposal entity adhering to Foundry Systems commercial rules.
    Initialized in DRAFT status.
    """
    business = db.query(Business).filter(Business.id == business_id).one()

    if package_type == PackageType.CAPABILITY_PORTAL_CORE.value:
        total = CORE_TOTAL_PRICE
        m1 = CORE_MILESTONE_1_MOBILIZATION
        m2 = CORE_MILESTONE_2_MIDWAY
        m3 = CORE_MILESTONE_3_LAUNCH
        retainer = CORE_CONTINUITY_RETAINER
    else:
        total = BASIC_TOTAL_PRICE
        m1 = BASIC_MILESTONE_1_MOBILIZATION
        m2 = BASIC_MILESTONE_2_MIDWAY
        m3 = BASIC_MILESTONE_3_LAUNCH
        retainer = BASIC_CONTINUITY_RETAINER

    proposal = Proposal(
        business_id=business.id,
        package_type=package_type,
        total_price=total,
        milestone_1_mobilization=m1,
        milestone_2_midway=m2,
        milestone_3_launch=m3,
        continuity_monthly_fee=retainer,
        status=ProposalStatus.DRAFT.value,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def issue_proposal(
    db: Session,
    proposal_id: str,
) -> Proposal:
    """Transition proposal from DRAFT to ISSUED."""
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).one()
    transition_proposal(
        db,
        proposal,
        ProposalStatus.ISSUED.value,
        actor="COMMERCIAL_SERVICE",
        reason="Grand Slam Proposal formally issued to prospect",
    )
    db.commit()
    return proposal


class GrandSlamProposalGenerator:
    """Class wrapper for creating and formatting proposals."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        business_id: str,
        package_type: str = PackageType.CAPABILITY_PORTAL_CORE.value,
    ) -> Proposal:
        return create_grand_slam_proposal(self.db, business_id, package_type)

    def issue(self, proposal_id: str) -> Proposal:
        return issue_proposal(self.db, proposal_id)

    def format_proposal(self, proposal: Proposal, business: Business) -> str:
        return format_proposal_markdown(proposal, business)
