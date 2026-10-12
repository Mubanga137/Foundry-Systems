"""
FS-CAE Acquisition Queue Router

Manages operator review of qualified contractors, yellow iron asset inspection,
$100M Leads copy review, 1-click batch approval, and safe dispatcher triggers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, Query, Request
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

        from app.services.outreach.adapters.messenger_adapter import extract_fb_username

        fb_url = biz.fb_page_url if biz else None
        username = extract_fb_username(fb_url or "") if fb_url else None
        messenger_url = f"https://m.me/{username}" if username else (fb_url or "https://m.me/")

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
                "fb_page_url": fb_url,
                "messenger_url": messenger_url,
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
    Only approves explicitly selected job IDs. If no IDs are provided,
    returns a 400 warning instead of approving all.
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

    # FIX 3: Accept raw UUID strings without prefix filtering.
    # Filter out empty or None values only.
    valid_job_ids = [j.strip() for j in (selected_ids or []) if j and j.strip()]

    # FIX 3: If no checkboxes were selected, do NOT approve all — return warning
    if not valid_job_ids:
        warn_msg = "No jobs selected for approval. Please select at least one job."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"status": "WARNING", "approved_count": 0, "message": warn_msg},
                status_code=400,
            )
        return RedirectResponse(url=f"/queue?error={warn_msg}", status_code=303)

    # Validate submitted IDs against actual DRAFTED jobs in DB
    valid_drafted = (
        db.query(DispatchJob.id)
        .filter(
            DispatchJob.id.in_(valid_job_ids),
            DispatchJob.status == DispatchStatus.DRAFTED.value,
        )
        .all()
    )
    validated_ids = [row[0] for row in valid_drafted]

    if not validated_ids:
        warn_msg = "None of the selected jobs are in DRAFTED status."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"status": "WARNING", "approved_count": 0, "message": warn_msg},
                status_code=400,
            )
        return RedirectResponse(url=f"/queue?error={warn_msg}", status_code=303)

    approved = approve_batch(db, job_ids=validated_ids)

    msg = f"Successfully approved {len(approved)} outreach jobs. Staged into QUEUED status."

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "SUCCESS", "approved_count": len(approved), "message": msg})

    return RedirectResponse(url=f"/queue?message={msg}", status_code=303)


@router.post("/dispatch")
def run_dispatcher(
    request: Request,
    force: bool = Query(False, description="Bypass pacing delay for immediate dispatch"),
    db: Session = Depends(get_db),
):
    """Trigger dispatch worker with operating hours gate and channel adaptation.

    Uses configured DISPATCH_MIN/MAX_DELAY_SECONDS for safe pacing.
    Pass ?force=true to bypass pacing (operator override for testing).
    """
    if force:
        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
    else:
        worker = DispatchWorker()  # Uses settings defaults (60-120s jitter)
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


@router.post("/jobs/{job_id}/update-message")
async def update_job_message(
    job_id: str,
    new_content: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Updates DispatchJob.rendered_content.
    Returns JSON {"success": True, "updated_content": new_content}.
    """
    job = db.query(DispatchJob).filter(DispatchJob.id == job_id).first()
    if not job:
        return JSONResponse({"success": False, "error": "Job not found"}, status_code=404)

    job.rendered_content = new_content.strip()
    db.commit()
    return JSONResponse({"success": True, "updated_content": job.rendered_content})


@router.post("/jobs/{job_id}/mark-sent")
def mark_job_sent(
    job_id: str,
    db: Session = Depends(get_db),
):
    """
    Updates job from QUEUED/DRAFTED to SENT, moves conversation to AWAITING_REPLY in /inbox,
    and records an outbound delivered message.
    """
    from datetime import datetime, timezone
    from app.models.conversation import Conversation, Message
    from app.schemas.states import ConversationState, DeliveryStatus, MessageDirection

    job = db.query(DispatchJob).filter(DispatchJob.id == job_id).first()
    if not job:
        return JSONResponse({"success": False, "error": "Job not found"}, status_code=404)

    now = datetime.now(timezone.utc)
    job.status = DispatchStatus.SENT.value
    job.sent_at = now
    job.delivered_at = now

    # Ensure conversation exists and is in AWAITING_REPLY
    conv = db.query(Conversation).filter(Conversation.business_id == job.business_id).first()
    if not conv:
        conv = Conversation(
            business_id=job.business_id,
            channel=job.channel or "FB_MESSENGER",
            state=ConversationState.AWAITING_REPLY.value,
            last_activity_at=now,
        )
        db.add(conv)
        db.flush()
    else:
        conv.state = ConversationState.AWAITING_REPLY.value
        conv.last_activity_at = now

    # Add outbound message
    msg = Message(
        conversation_id=conv.id,
        direction=MessageDirection.OUTBOUND.value,
        content=job.rendered_content,
        created_at=now,
        delivery_status=DeliveryStatus.SENT.value,
    )
    db.add(msg)
    db.commit()

    return JSONResponse({
        "success": True,
        "job_id": job.id,
        "status": job.status,
        "conversation_id": conv.id,
        "conversation_state": conv.state,
    })
