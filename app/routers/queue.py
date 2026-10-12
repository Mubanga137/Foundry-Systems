"""
FS-CAE Acquisition Queue Router

Manages operator review of qualified contractors, yellow iron asset inspection,
$100M Leads copy review, 1-click batch approval, and safe dispatcher triggers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import DispatchStatus, QualificationState
from app.services.outreach.batch_manager import (
    approve_batch,
    determine_default_channel,
    draft_outreach_for_qualified,
)
from app.services.outreach.dispatcher import DispatchWorker, is_within_operating_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue")
templates = Jinja2Templates(directory="app/templates")


def _extract_assets(asset_footprint: Any) -> List[str]:
    """Helper to extract flat tag list from asset_footprint structure."""
    if not asset_footprint:
        return []
    if isinstance(asset_footprint, list):
        return [str(a) for a in asset_footprint[:4]]
    if isinstance(asset_footprint, dict):
        tags = []
        for v in asset_footprint.values():
            if isinstance(v, list):
                tags.extend([str(item) for item in v])
            elif isinstance(v, str):
                tags.append(v)
        return tags[:4]
    return [str(asset_footprint)[:30]]


@router.get("", response_class=HTMLResponse)
def get_queue(
    request: Request,
    message: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # Auto-draft jobs for any newly qualified prospects so they are ready for approval
    try:
        draft_outreach_for_qualified(db, limit=20)
    except Exception as e:
        logger.warning("Auto-draft outreach notice: %s", e)

    # Fetch all dispatch jobs that are DRAFTED, QUEUED, or SENDING
    active_jobs = (
        db.query(DispatchJob)
        .join(Business, DispatchJob.business_id == Business.id)
        .filter(
            DispatchJob.status.in_([
                DispatchStatus.DRAFTED.value,
                DispatchStatus.QUEUED.value,
                DispatchStatus.SENDING.value,
            ])
        )
        .order_by(DispatchJob.created_at.desc())
        .all()
    )

    items = []
    staged_count = 0
    queued_count = 0

    for job in active_jobs:
        biz = job.business
        qual = biz.qualification if biz else None
        fit_score = qual.fit_score if qual else 70
        assets = _extract_assets(qual.asset_footprint if qual else None)

        if job.status == DispatchStatus.DRAFTED.value:
            staged_count += 1
        elif job.status == DispatchStatus.QUEUED.value:
            queued_count += 1

        items.append(
            {
                "job_id": job.id,
                "business_id": biz.id if biz else "",
                "company_name": biz.company_name if biz else "Contractor",
                "location": biz.location_district if biz else "Zambia",
                "phone": biz.phone_normalized if biz else "",
                "fit_score": fit_score,
                "assets": assets,
                "message_body": job.rendered_content,
                "channel": job.channel,
                "status": job.status,
            }
        )

    # If no jobs exist, also display any raw QUALIFIED prospects
    if not items:
        qualifications = (
            db.query(ProspectQualification)
            .join(Business, ProspectQualification.business_id == Business.id)
            .filter(
                ProspectQualification.qualification_state.in_([
                    QualificationState.QUALIFIED.value,
                    QualificationState.STAGED_FOR_OUTREACH.value,
                ]),
                Business.is_suppressed.is_(False),
            )
            .limit(50)
            .all()
        )
        for qual in qualifications:
            biz = qual.business
            channel, _ = determine_default_channel(biz)
            items.append(
                {
                    "job_id": None,
                    "business_id": biz.id,
                    "company_name": biz.company_name,
                    "location": biz.location_district,
                    "phone": biz.phone_normalized,
                    "fit_score": qual.fit_score,
                    "assets": _extract_assets(qual.asset_footprint),
                    "message_body": "Ready to compile $100M Leads message",
                    "channel": channel,
                    "status": qual.qualification_state,
                }
            )

    dispatch_active = is_within_operating_hours()

    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "active_page": "queue",
            "items": items,
            "staged_count": staged_count,
            "queued_count": queued_count,
            "dispatch_active": dispatch_active,
            "message": message,
            "error": error,
        },
    )


@router.post("/approve")
async def approve_queue_batch(
    request: Request,
    job_ids: Optional[List[str]] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Approve selected drafted jobs and move them to QUEUED.
    Supports form submission as well as JSON payload for API / HTMX.
    """
    selected_ids = job_ids

    # Handle JSON payload if requested
    if selected_ids is None and request.headers.get("content-type") == "application/json":
        try:
            body = await request.json()
            selected_ids = body.get("job_ids")
        except Exception:
            pass

    # Ensure any un-drafted prospects get drafted first
    try:
        draft_outreach_for_qualified(db, limit=20)
    except Exception:
        pass

    # Filter out empty or None values
    valid_job_ids = [j for j in (selected_ids or []) if j and j.startswith("job-")]
    business_ids = [j for j in (selected_ids or []) if j and j.startswith("biz-")]

    # If business_ids were passed, lookup corresponding drafted job ids
    if business_ids:
        b_jobs = (
            db.query(DispatchJob.id)
            .filter(
                DispatchJob.business_id.in_(business_ids),
                DispatchJob.status == DispatchStatus.DRAFTED.value,
            )
            .all()
        )
        valid_job_ids.extend([bj[0] for bj in b_jobs])

    # If no specific IDs were submitted, approve all currently drafted jobs up to 20
    approve_target = valid_job_ids if valid_job_ids else None
    approved = approve_batch(db, job_ids=approve_target)

    msg = f"Successfully approved {len(approved)} outreach jobs. Staged into QUEUED status."

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "SUCCESS", "approved_count": len(approved), "message": msg})

    return RedirectResponse(url=f"/queue?message={msg}", status_code=303)


@router.post("/dispatch")
def run_dispatcher(
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger dispatch worker with operating hours gate and channel adaptation."""
    worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
    summary = worker.dispatch_queue(db, limit=20)

    if summary.held_by_operating_hours:
        msg = "Safe Dispatcher is HELD: Outside active CAT operating hours (08:00 - 16:30)."
    elif summary.dispatched > 0:
        msg = f"Safe Dispatcher executed: {summary.dispatched} sent, {summary.blocked} blocked/assisted."
    else:
        msg = f"Dispatcher run complete: {summary.total_processed} processed, 0 sent."

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({
            "status": "COMPLETED",
            "dispatched": summary.dispatched,
            "blocked": summary.blocked,
            "message": msg,
        })

    return RedirectResponse(url=f"/queue?message={msg}", status_code=303)
