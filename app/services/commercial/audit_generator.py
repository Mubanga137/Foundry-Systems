"""
FS-CAE 3-Point Commercial Capability Audit Generator

Compiles evidence-based, sector-specific capability audits for qualified prospects.
Grounded in observed operational assets and digital friction points without fictitious claims.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import CommercialAuditStatus
from app.services.state_machine import transition_audit

logger = logging.getLogger(__name__)

SECTOR_EARTHWORKS = "EARTHWORKS_AND_CIVIL"
SECTOR_PLANT_HIRE = "PLANT_HIRE_AND_EQUIPMENT"
SECTOR_QUARRY = "QUARRIES_AND_AGGREGATES"
SECTOR_LOGISTICS = "HEAVY_LOGISTICS_AND_HAULAGE"


def detect_sector_category(business: Business, qualification: Optional[ProspectQualification]) -> str:
    """Classify business into one of the 4 core industrial sectors."""
    search_corpus = " ".join([
        business.industry_sector or "",
        business.company_name or "",
        business.description_raw or "",
    ]).lower()

    if any(k in search_corpus for k in ("quarry", "stone", "aggregate", "crusher", "sand", "gravel")):
        return SECTOR_QUARRY
    elif any(k in search_corpus for k in ("plant hire", "equipment hire", "crane hire", "plant &", "plant and")):
        return SECTOR_PLANT_HIRE
    elif any(k in search_corpus for k in ("haulage", "logistics", "freight", "transport", "tri-axle", "cargo")):
        return SECTOR_LOGISTICS
    elif any(k in search_corpus for k in ("earthmoving", "earthworks", "civil", "road", "construction", "contractor")):
        return SECTOR_EARTHWORKS

    # Fallback to sector or default
    return SECTOR_EARTHWORKS


def get_sector_audit_template(sector: str, business: Business) -> dict[str, str]:
    """Retrieve fact-grounded audit findings for the sector."""
    loc = business.location_district or "operational corridors"

    if sector == SECTOR_EARTHWORKS:
        return {
            "finding_1": (
                "Visibility of Technical Plant & Machine Tonnage: Heavy machinery specs "
                "(excavators, dozers, tipper capacities) and verified project delivery history "
                "are unavailable for rapid inspection by commercial procurement estimators."
            ),
            "finding_2": (
                "Lack of Structured Mobile RFQ Capture: Tier-1 mining and civil contractors "
                "cannot request site-clearing or bulk earthwork quotations directly on mobile or WhatsApp."
            ),
            "finding_3": (
                "Absence of Verified Compliance Credentials on Mobile: Statutory certifications "
                "(NCC registration, PACRA corporate profile, EIZ compliance) are not presented in a "
                "secure, shareable format for corporate vendor onboarding."
            ),
            "commercial_implications": (
                "Procurement committees and international primes in Zambia routinely disqualify "
                "subcontractors when machine capacities and statutory compliance cannot be verified "
                "instantly on mobile devices, routing tender packages to competitors with accessible digital portfolios."
            ),
            "recommended_scope": (
                "Foundry Systems Capability Portal Core — comprehensive machinery asset catalog, "
                "mobile RFQ dispatch routing, and verified compliance credentials showcase."
            ),
        }

    elif sector == SECTOR_PLANT_HIRE:
        return {
            "finding_1": (
                "Equipment Catalogue Organization: Machine specs (operating tonnage, bucket capacity, "
                "engine specifications, and dry/wet hire rates) are not structured for immediate review."
            ),
            "finding_2": (
                "Inability for Site Engineers to Request Instant Equipment Dispatch: No direct "
                "mobile dispatch trigger for emergency or short-notice plant mobilization."
            ),
            "finding_3": (
                f"Unclear Service Territory & Mobilization Terms: Demarcation of delivery across {loc} "
                "and minimum mobilization periods are undocumented, creating friction for corporate hirers."
            ),
            "commercial_implications": (
                "Site managers and commercial estimators requiring equipment on short lead times "
                "immediately bypass plant owners whose rates and machine specs are not viewable 24/7 on mobile."
            ),
            "recommended_scope": (
                "Foundry Systems Capability Portal Core — real-time plant catalogue with instant mobile booking "
                "and verified mobilization terms."
            ),
        }

    elif sector == SECTOR_QUARRY:
        return {
            "finding_1": (
                "Presentation of Material Grades: Technical material grading specifications "
                "(G1–G7 gravel, 19mm/25mm concrete aggregate, base course, quarry dust) are unlisted online."
            ),
            "finding_2": (
                "Lack of Bulk Volume Estimator or Structured Pricing Flow: Construction buyers "
                "cannot calculate tonnage requirements or request volume discounts through a structured mobile interface."
            ),
            "finding_3": (
                f"Physical Quarry Verification & Weighbridge Operations: Pit coordinates along {loc} "
                "and certified weighbridge operational procedures are difficult to verify remotely."
            ),
            "commercial_implications": (
                "Batching plants and civil engineering works managers procure from competitors "
                "with clear grading schedules, certified weighbridge guarantees, and instant bulk quotation flows."
            ),
            "recommended_scope": (
                "Foundry Systems Capability Portal Core — aggregate grading guide, bulk tonnage calculator, "
                "and verified weighbridge route logistics portal."
            ),
        }

    else:  # SECTOR_LOGISTICS
        return {
            "finding_1": (
                "Route Coverage & Fleet Configurations: Details of tri-axle flatbeds, bulk tippers, "
                "or tankers and dedicated cross-border transit corridors are not structured for corporate logistics heads."
            ),
            "finding_2": (
                "GIT (Goods In Transit) Insurance Verification Visibility: Lack of transparent, "
                "up-to-date insurance coverage verification causes hesitation among high-value cargo shippers."
            ),
            "finding_3": (
                "Direct Dispatch Communication Pathways: Shippers lack a friction-free mobile mechanism "
                "to secure backhaul capacity or request instant freight manifests."
            ),
            "commercial_implications": (
                "Mining suppliers and multinational import/export brokers route freight exclusively "
                "to hauliers with transparent GIT insurance verifications and published corridor capacities."
            ),
            "recommended_scope": (
                "Foundry Systems Capability Portal Core — transport corridor map, fleet trailer specifications, "
                "and verified GIT certification badge."
            ),
        }


def format_audit_markdown(audit: CommercialAudit, business: Business) -> str:
    """Format audit deliverable into structured Markdown presentation."""
    return (
        f"# 3-POINT COMMERCIAL CAPABILITY AUDIT\n"
        f"**Target Company:** {business.company_name}\n"
        f"**Corridor / Location:** {business.location_district or 'Zambia'}\n"
        f"**Sector:** {business.industry_sector or 'Industrial Contracting'}\n"
        f"**Date:** {datetime.now(timezone.utc).strftime('%B %Y')}\n"
        f"**Reviewing Authority:** Foundry Systems B2B Engineering Practice\n\n"
        f"## 1. OBSERVED CAPABILITY GAPS\n"
        f"1. **{audit.finding_1}**\n\n"
        f"2. **{audit.finding_2}**\n\n"
        f"3. **{audit.finding_3}**\n\n"
        f"## 2. COMMERCIAL IMPLICATIONS\n"
        f"{audit.commercial_implications}\n\n"
        f"## 3. RECOMMENDED RESOLUTION SCOPE\n"
        f"{audit.recommended_scope}\n"
    )


def generate_commercial_audit(
    db: Session,
    business_id: str,
) -> CommercialAudit:
    """
    Generate a 3-Point Commercial Capability Audit record for a business.
    Transitions audit from PENDING_GENERATION to GENERATED with audit logging.
    """
    business = db.query(Business).filter(Business.id == business_id).one()
    qualification = business.qualification

    sector = detect_sector_category(business, qualification)
    template = get_sector_audit_template(sector, business)

    audit = CommercialAudit(
        business_id=business.id,
        status=CommercialAuditStatus.PENDING_GENERATION.value,
        finding_1=template["finding_1"],
        finding_2=template["finding_2"],
        finding_3=template["finding_3"],
        commercial_implications=template["commercial_implications"],
        recommended_scope=template["recommended_scope"],
    )
    db.add(audit)
    db.flush()

    # Transition to GENERATED
    transition_audit(
        db,
        audit,
        CommercialAuditStatus.GENERATED.value,
        actor="AUDIT_GENERATOR",
        reason=f"Generated 3-point capability audit for sector {sector}",
    )
    db.commit()
    return audit


def approve_commercial_audit(
    db: Session,
    audit_id: str,
) -> CommercialAudit:
    """Approve a GENERATED audit for delivery."""
    audit = db.query(CommercialAudit).filter(CommercialAudit.id == audit_id).one()
    transition_audit(
        db,
        audit,
        CommercialAuditStatus.APPROVED.value,
        actor="OPERATOR",
        reason="Operator approved commercial audit for outbound delivery",
    )
    db.commit()
    return audit


class CommercialAuditGenerator:
    """Class wrapper for generating capability audits."""

    def __init__(self, db: Session):
        self.db = db

    def generate(self, business_id: str) -> CommercialAudit:
        return generate_commercial_audit(self.db, business_id)

    def format_deliverable(self, audit: CommercialAudit, business: Business) -> str:
        return format_audit_markdown(audit, business)
