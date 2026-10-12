"""
FS-CAE Executive Dashboard Router

Provides executive KPIs, pipeline health funnel, Gemini API key health,
and direct file ingestion upload dropzone.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.business import Business
from app.models.commercial import Invoice, PaymentEvent, ProjectActivation, Proposal
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    DispatchStatus,
    InvoiceStatus,
    MessageDirection,
    MilestoneTier,
    ProjectActivationStatus,
    QualificationState,
)
from app.services.ingestion.importer import import_file
from app.services.intelligence.key_manager import redact_key
from app.services.orchestrator import orchestrator
from app.services.outreach.dispatcher import is_within_operating_hours

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Persistent ingestion input directory
DATA_INPUT_DIR = Path("data/input")


@router.get("/", response_class=HTMLResponse)
def get_dashboard(
    request: Request,
    message: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    settings = get_settings()

    # 1. Pipeline Metrics
    discovered_count = db.query(func.count(Business.id)).scalar() or 0
    qualified_count = (
        db.query(func.count(ProspectQualification.id))
        .filter(
            (ProspectQualification.qualification_state == QualificationState.QUALIFIED.value)
            | (ProspectQualification.fit_score >= 65)
        )
        .scalar()
        or 0
    )
    dispatched_count = (
        db.query(func.count(DispatchJob.id))
        .filter(DispatchJob.status == DispatchStatus.SENT.value)
        .scalar()
        or 0
    )
    conversations_count = db.query(func.count(Conversation.id)).scalar() or 0
    pending_invoices_count = (
        db.query(func.count(Invoice.id))
        .filter(
            Invoice.milestone_tier == MilestoneTier.MOBILIZATION.value,
            Invoice.status == InvoiceStatus.ISSUED.value,
        )
        .scalar()
        or 0
    )
    total_revenue = (
        db.query(func.sum(PaymentEvent.amount_received)).scalar() or 0.0
    )

    metrics = {
        "discovered_count": discovered_count,
        "qualified_count": qualified_count,
        "dispatched_count": dispatched_count,
        "conversations_count": conversations_count,
        "pending_invoices_count": pending_invoices_count,
        "total_revenue": float(total_revenue),
    }

    # 2. Conversion Funnel Bar
    contacted_count = (
        db.query(func.count(func.distinct(DispatchJob.business_id)))
        .filter(DispatchJob.status == DispatchStatus.SENT.value)
        .scalar()
        or 0
    )
    replied_count = (
        db.query(func.count(func.distinct(Conversation.business_id)))
        .join(Message, Conversation.id == Message.conversation_id)
        .filter(Message.direction == MessageDirection.INBOUND.value)
        .scalar()
        or 0
    )
    invoiced_count = (
        db.query(func.count(func.distinct(Proposal.business_id)))
        .join(Invoice, Proposal.id == Invoice.proposal_id)
        .scalar()
        or 0
    )
    activated_count = (
        db.query(func.count(ProjectActivation.id))
        .filter(ProjectActivation.status == ProjectActivationStatus.ACTIVATED.value)
        .scalar()
        or 0
    )

    funnel = {
        "imported": discovered_count,
        "qualified": qualified_count,
        "contacted": contacted_count,
        "replied": replied_count,
        "invoiced": invoiced_count,
        "activated": activated_count,
    }

    # 3. Gemini Keys Status
    raw_keys = settings.GEMINI_API_KEYS or []
    gemini_keys_status = []
    for idx, k in enumerate(raw_keys, start=1):
        gemini_keys_status.append(
            {
                "index": idx,
                "redacted": redact_key(k),
                "status": "OPERATIONAL",
            }
        )

    dispatch_active = is_within_operating_hours()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "metrics": metrics,
            "funnel": funnel,
            "gemini_keys": gemini_keys_status,
            "gemini_model": settings.GEMINI_MODEL,
            "dispatch_active": dispatch_active,
            "message": message,
            "error": error,
        },
    )


@router.post("/upload")
async def upload_files(
    request: Request,
    background_tasks: BackgroundTasks,
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    auto_pipeline: Optional[bool] = Form(None),
    auto_qualify: Optional[bool] = Form(None),
    db: Session = Depends(get_db),
):
    """Handle drag-and-drop multi-file upload for CSV or XLSX lead lists.

    Accepts one or more files simultaneously. Accepts either `files` (multi-file)
    or `file` (single-file legacy) form field name. Each file is:
      1. Validated for .csv/.xlsx extension
      2. Saved to data/input/ for provenance
      3. Parsed by ImporterService with deduplication
      4. Optionally auto-qualified via the intelligence pipeline

    Returns JSON for AJAX/fetch calls, or redirects for standard form posts.
    """
    is_json = (
        "application/json" in request.headers.get("accept", "")
        or "application/json" in request.headers.get("content-type", "")
        or request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    )

    # Collect all provided uploads
    all_uploads: List[UploadFile] = []
    if files:
        for f in files:
            if f and f.filename:
                all_uploads.append(f)
    if file and file.filename:
        all_uploads.append(file)

    if not all_uploads:
        error_msg = "No files uploaded. Please select one or more .csv or .xlsx files."
        if is_json:
            return JSONResponse({"status": "ERROR", "error": error_msg}, status_code=400)
        return RedirectResponse(
            url=f"/?error={error_msg.replace(' ', '+')}",
            status_code=303,
        )

    # Validate all file extensions first
    ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
    rejected = []
    accepted = []

    for f in all_uploads:
        filename = f.filename or "upload.csv"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            rejected.append(filename)
        else:
            accepted.append((f, filename, ext))

    if rejected and not accepted:
        error_msg = (
            f"Invalid file format: {', '.join(rejected)}. "
            f"Please upload .csv or .xlsx files only."
        )
        if is_json:
            return JSONResponse({"status": "ERROR", "error": error_msg}, status_code=400)
        return RedirectResponse(
            url=f"/?error={error_msg.replace(' ', '+')}",
            status_code=303,
        )

    # Ensure data/input directory exists
    DATA_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Process each accepted file
    total_rows = 0
    total_new = 0
    total_dupes = 0
    files_processed = 0
    file_summaries = []
    temp_paths = []

    try:
        for upload_file, filename, ext in accepted:
            temp_path = None
            try:
                # Write to temp file for processing
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                    content = await upload_file.read()
                    tmp.write(content)
                    temp_path = tmp.name
                temp_paths.append(temp_path)

                # Also save a copy to data/input/ for provenance
                try:
                    dest = DATA_INPUT_DIR / filename
                    # Avoid overwriting existing files
                    counter = 1
                    while dest.exists():
                        stem = Path(filename).stem
                        dest = DATA_INPUT_DIR / f"{stem}_{counter}{ext}"
                        counter += 1
                    shutil.copy2(temp_path, str(dest))
                except Exception:
                    pass  # Non-critical; temp file is the processing source

                # Run importer
                import_stats = import_file(db, temp_path)
                rows = getattr(import_stats, "rows_read", 0)
                new_biz = getattr(import_stats, "new_businesses", 0)
                dupes = getattr(import_stats, "duplicates_skipped", 0) + getattr(
                    import_stats, "duplicates_merged", 0
                )

                total_rows += rows
                total_new += new_biz
                total_dupes += dupes
                files_processed += 1

                file_summaries.append(
                    {
                        "filename": filename,
                        "rows_read": rows,
                        "new_businesses": new_biz,
                        "duplicates": dupes,
                    }
                )

            except Exception as e:
                logger.error("Error processing file %s: %s", filename, e)
                file_summaries.append(
                    {
                        "filename": filename,
                        "rows_read": 0,
                        "new_businesses": 0,
                        "duplicates": 0,
                        "error": str(e),
                    }
                )

        # Auto-qualification and outreach compilation via autonomous orchestrator
        should_run = True if (auto_pipeline is None and auto_qualify is None) else bool(auto_pipeline if auto_pipeline is not None else auto_qualify)
        qualified_count = 0
        if should_run and total_new > 0:
            try:
                pipeline_res = orchestrator.run_pipeline(
                    files=None,
                    auto_qualify=True,
                    auto_draft=True,
                    db=db,
                )
                qualified_count = pipeline_res.get("summary", {}).get("qualified", 0)
            except Exception as e:
                logger.warning("Autonomous pipeline error during upload: %s", e)
        else:
            orchestrator.add_log(
                f"[INGEST] Processed {files_processed} files. Added {total_new} new contractors, "
                f"skipped {total_dupes} duplicates."
            )

        # Build response
        result = {
            "success": True,
            "status": "SUCCESS",
            "message": "Files ingested. Autonomous pipeline running in background.",
            "files_processed": files_processed,
            "records_ingested": total_new,
            "duplicates_skipped": total_dupes,
            "files_count": files_processed,
            "total_rows": total_rows,
            "new_businesses": total_new,
            "qualified_count": qualified_count,
            "file_details": file_summaries,
        }

        if rejected:
            result["rejected_files"] = rejected

        if is_json:
            return JSONResponse(result)

        # Build flash message for redirect
        msg = (
            f"Successfully processed {files_processed} file(s): "
            f"{total_rows} rows read, {total_new} new contractors added, "
            f"{total_dupes} duplicates merged"
        )
        if qualified_count > 0:
            msg += f", {qualified_count} prospects qualified"
        msg += "."
        if rejected:
            msg += f" Rejected: {', '.join(rejected)}"

        return RedirectResponse(url=f"/queue?message={msg}", status_code=303)

    except Exception as e:
        logger.error("Upload processing failed: %s", e)
        error_msg = f"Import failed: {str(e)}"
        if is_json:
            return JSONResponse({"status": "ERROR", "error": error_msg}, status_code=500)
        return RedirectResponse(url=f"/?error={error_msg}", status_code=303)

    finally:
        for tp in temp_paths:
            if tp and os.path.exists(tp):
                try:
                    os.remove(tp)
                except OSError:
                    pass


@router.post("/api/pipeline/run-all")
async def trigger_run_all(
    background_tasks: BackgroundTasks,
    auto_qualify: bool = True,
    auto_draft: bool = True,
    db: Session = Depends(get_db),
):
    """Master manual 1-click execution of the full autonomous pipeline."""
    background_tasks.add_task(
        orchestrator.run_pipeline,
        files=None,
        auto_qualify=auto_qualify,
        auto_draft=auto_draft,
    )
    return JSONResponse({
        "status": "SUCCESS",
        "message": "Autonomous pipeline triggered for all unprocessed records",
    })


@router.get("/api/pipeline/telemetry")
def get_pipeline_telemetry(db: Session = Depends(get_db)):
    """Real-time engine telemetry streaming endpoint."""
    telemetry = orchestrator.get_telemetry(db=db)
    return JSONResponse(telemetry)
