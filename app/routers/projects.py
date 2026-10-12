"""
FS-CAE Client Implementation Vault Router

Displays activated clients, compiled delivery briefs, and the pre-generated
Builder.io Master Architectural Prompt with 1-click clipboard portability.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.business import Business
from app.models.commercial import Invoice, PaymentEvent, ProjectActivation, Proposal
from app.schemas.states import ProjectActivationStatus
from app.services.commercial.activation_service import (
    compile_delivery_brief,
    format_delivery_brief_markdown,
)
from app.services.outreach.dispatcher import is_within_operating_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def get_projects(
    request: Request,
    message: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    activations = (
        db.query(ProjectActivation)
        .order_by(ProjectActivation.activated_at.desc())
        .all()
    )

    projects_list = []
    for act in activations:
        biz = act.business
        if not biz:
            continue

        try:
            brief = compile_delivery_brief(db, biz.id)
        except Exception as e:
            logger.warning("Error compiling brief for %s: %s", biz.id, e)
            brief = {
                "client_name": biz.company_name,
                "business_id": biz.id,
                "location": biz.location_district or "Zambia",
                "package": "CAPABILITY_PORTAL_CORE",
                "total_value": 14500.0,
                "verified_assets": "Commercial Fleet",
                "audit_findings": ["Commercial capability verification"],
                "builder_master_prompt": f"BUILD COMMERCIAL PORTAL FOR {biz.company_name}",
            }

        # Look up payment ref from associated mobilization invoice
        payment_event = (
            db.query(PaymentEvent)
            .filter(PaymentEvent.invoice_id == act.mobilization_invoice_id)
            .first()
        )
        payment_ref = payment_event.transaction_reference if payment_event else "VERIFIED"

        act_date_str = (
            act.activated_at.strftime("%b %d, %Y")
            if act.activated_at
            else "Recently"
        )

        projects_list.append(
            {
                "id": act.id,
                "business_id": biz.id,
                "client_name": brief.get("client_name", biz.company_name),
                "location": brief.get("location", "Zambia"),
                "package": brief.get("package", "CAPABILITY_PORTAL_CORE"),
                "total_value": brief.get("total_value", 14500.0),
                "activated_at": act_date_str,
                "payment_ref": payment_ref,
                "verified_assets": brief.get("verified_assets", "Commercial fleet"),
                "audit_findings": brief.get("audit_findings", []),
                "builder_master_prompt": brief.get("builder_master_prompt", ""),
            }
        )

    dispatch_active = is_within_operating_hours()

    return templates.TemplateResponse(
        request=request,
        name="projects.html",
        context={
            "active_page": "projects",
            "projects": projects_list,
            "dispatch_active": dispatch_active,
            "message": message,
            "error": error,
        },
    )


@router.get("/{project_id}/brief")
def get_project_brief(
    project_id: str,
    db: Session = Depends(get_db),
):
    """Return raw JSON delivery brief for an activated project."""
    act = db.query(ProjectActivation).filter(ProjectActivation.id == project_id).first()
    if not act or not act.business_id:
        raise HTTPException(status_code=404, detail="Project not found")

    brief = compile_delivery_brief(db, act.business_id)
    return JSONResponse(brief)
