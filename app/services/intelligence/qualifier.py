"""
Qualification Pipeline Orchestrator

Drives the full pipeline for DISCOVERED businesses:
  1. Transition to EVALUATING via Layer 1 state machine.
  2. Run Gemini analysis + deterministic scoring.
  3. Populate ProspectQualification fields.
  4. Transition to QUALIFIED or DISQUALIFIED via Layer 1 state machine.
  5. AuditLog entries are auto-recorded by Layer 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import QualificationState
from app.services.intelligence.gemini_client import GeminiAnalysisClient
from app.services.intelligence.scorer import compute_score
from app.services.state_machine import transition_qualification

logger = logging.getLogger(__name__)


@dataclass
class QualificationSummary:
    """Summary of a qualification batch run."""
    total_processed: int = 0
    qualified: int = 0
    disqualified: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


def qualify_business(
    session: Session,
    business: Business,
    qualification: ProspectQualification,
    gemini_client: GeminiAnalysisClient,
) -> str:
    """
    Run the full qualification pipeline for a single business.

    Returns the final qualification state string.
    """
    # Step 1: Transition DISCOVERED → EVALUATING
    if qualification.qualification_state == QualificationState.DISCOVERED.value:
        transition_qualification(
            session, qualification,
            QualificationState.EVALUATING.value,
            actor="SYSTEM",
            reason="Starting Gemini evaluation pipeline",
        )

    # Step 2: Run Gemini analysis
    biz_data = {
        "company_name": business.company_name,
        "industry_sector": business.industry_sector,
        "description_raw": business.description_raw,
        "location_district": business.location_district,
        "website_url": business.website_url,
        "fb_page_url": business.fb_page_url,
        "phone_normalized": business.phone_normalized,
    }
    analysis = gemini_client.analyze(biz_data)

    # Step 3: Deterministic scoring
    has_website = bool(business.website_url)
    has_fb_messenger = bool(
        business.fb_page_url
        and ("m.me/" in business.fb_page_url or "messenger" in business.fb_page_url.lower())
    )

    score_result = compute_score(
        analysis,
        has_website=has_website,
        has_fb_messenger=has_fb_messenger,
        phone_normalized=business.phone_normalized,
    )

    # Step 4: Populate qualification fields
    qualification.fit_score = score_result.total_score
    qualification.sector_match = score_result.sector_match
    qualification.asset_footprint = analysis.get("asset_footprint", [])
    qualification.observed_friction_points = analysis.get("digital_deficits", [])

    # Step 5: Transition to final state
    if score_result.is_qualified:
        target_state = QualificationState.QUALIFIED.value
        transition_qualification(
            session, qualification, target_state,
            fit_score=score_result.total_score,
            actor="SYSTEM",
            reason=f"Score {score_result.total_score}/100 — "
                   f"S:{score_result.pillar_1_sector} "
                   f"A:{score_result.pillar_2_assets} "
                   f"D:{score_result.pillar_3_digital} "
                   f"C:{score_result.pillar_4_contact}",
        )
    else:
        target_state = QualificationState.DISQUALIFIED.value
        disq_reasons = []
        if not score_result.sector_match:
            disq_reasons.append("Sector mismatch")
        if score_result.total_score < 65:
            disq_reasons.append(f"Score {score_result.total_score}/100 below threshold")
        if score_result.disqualification_flags:
            disq_reasons.extend(score_result.disqualification_flags)

        transition_qualification(
            session, qualification, target_state,
            fit_score=score_result.total_score,
            disqualification_reason="; ".join(disq_reasons),
            actor="SYSTEM",
            reason=f"Score {score_result.total_score}/100 — "
                   f"S:{score_result.pillar_1_sector} "
                   f"A:{score_result.pillar_2_assets} "
                   f"D:{score_result.pillar_3_digital} "
                   f"C:{score_result.pillar_4_contact}",
        )

    session.flush()
    return target_state


def qualify_batch(
    session: Session,
    gemini_client: GeminiAnalysisClient,
    limit: Optional[int] = None,
) -> QualificationSummary:
    """
    Process all DISCOVERED businesses through the qualification pipeline.

    Args:
        session: Database session.
        gemini_client: Configured Gemini client.
        limit: Maximum number of businesses to process (None = all).

    Returns:
        QualificationSummary with counts and any error details.
    """
    summary = QualificationSummary()

    query = (
        session.query(ProspectQualification)
        .filter(
            ProspectQualification.qualification_state == QualificationState.DISCOVERED.value
        )
    )
    if limit:
        query = query.limit(limit)

    prospects = query.all()

    for qual in prospects:
        try:
            business = qual.business
            if not business:
                summary.errors += 1
                summary.error_details.append(
                    f"Qualification {qual.id} has no linked business"
                )
                continue

            result_state = qualify_business(
                session, business, qual, gemini_client
            )

            if result_state == QualificationState.QUALIFIED.value:
                summary.qualified += 1
            else:
                summary.disqualified += 1

            summary.total_processed += 1

        except Exception as e:
            summary.errors += 1
            summary.error_details.append(
                f"Business {qual.business_id}: {str(e)}"
            )
            logger.exception(
                "Error qualifying business %s", qual.business_id
            )

    session.commit()
    return summary
