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

    Saves uploaded files to data/input/, returns an immediate HTTP 202 response,
    then offloads parsing, import, and pipeline execution to a background worker
    so the event loop is never blocked and telemetry polling continues uninterrupted.
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

    # ---- Save files to disk synchronously (fast I/O, non-blocking safe) ----
    saved_paths = []
    for upload_file, filename, ext in accepted:
        try:
            content = await upload_file.read()
            dest = DATA_INPUT_DIR / filename
            counter = 1
            while dest.exists():
                stem = Path(filename).stem
                dest = DATA_INPUT_DIR / f"{stem}_{counter}{ext}"
                counter += 1
            dest.write_bytes(content)
            saved_paths.append(str(dest))
        except Exception as e:
            logger.error("Error saving file %s: %s", filename, e)

    if not saved_paths:
        error_msg = "Failed to save uploaded files to disk."
        if is_json:
            return JSONResponse({"status": "ERROR", "error": error_msg}, status_code=500)
        return RedirectResponse(url=f"/?error={error_msg}", status_code=303)

    # ---- Determine pipeline behavior ----
    should_run = True if (auto_pipeline is None and auto_qualify is None) else bool(
        auto_pipeline if auto_pipeline is not None else auto_qualify
    )

    # ---- Ingest files into DB and collect statistics ----
    total_new = 0
    total_dupes = 0
    total_rows = 0
    files_processed = 0
    all_errors = []
    file_summaries = []

    for path_str in saved_paths:
        try:
            import_stats = import_file(db, path_str)
            rows = getattr(import_stats, "rows_read", 0)
            new_biz = getattr(import_stats, "new_businesses", 0)
            dupes = getattr(import_stats, "duplicates_skipped", 0) + getattr(
                import_stats, "duplicates_merged", 0
            )
            errors = getattr(import_stats, "errors", [])

            total_rows += rows
            total_new += new_biz
            total_dupes += dupes
            files_processed += 1

            if errors:
                all_errors.extend(errors)

            file_summaries.append(
                {
                    "filename": Path(path_str).name,
                    "rows_read": rows,
                    "new_businesses": new_biz,
                    "duplicates": dupes,
                }
            )

            # Surface ingestion warnings in telemetry
            if new_biz == 0 and errors:
                error_detail = "; ".join(errors[:3])
                orchestrator.add_log(
                    f"[INGEST] ⚠ WARNING: {Path(path_str).name} — 0 new records. "
                    f"Errors: {error_detail}"
                )
            else:
                orchestrator.add_log(
                    f"[INGEST] Parsed {Path(path_str).name}: {rows} rows, "
                    f"{new_biz} new, {dupes} duplicates"
                )
        except Exception as ie:
            logger.error("Error importing %s: %s", path_str, ie)
            orchestrator.add_log(f"[INGEST] ✕ Error importing {Path(path_str).name}: {ie}")
            all_errors.append(str(ie))

    # FIX 2: Surface ingestion errors if no businesses created and errors occurred
    if total_new == 0 and all_errors:
        err_msg = f"Ingestion Failed: {'; '.join(all_errors[:3])}"
        orchestrator.add_log(f"[INGEST] ❌ {err_msg}")
        if is_json:
            return JSONResponse(
                {
                    "status": "ERROR",
                    "error": err_msg,
                    "errors": all_errors,
                    "files_count": len(saved_paths),
                    "new_businesses": 0,
                },
                status_code=400,
            )
        return RedirectResponse(
            url=f"/?error={err_msg.replace(' ', '+')}",
            status_code=303,
        )

    # Telemetry log for successful ingress
    orchestrator.add_log(
        f"[INGEST] ✓ Ingestion complete: {files_processed} file(s), "
        f"{total_new} new contractors, {total_dupes} duplicates skipped."
    )

    # FIX 1: Offload AI pipeline to background worker thread so event loop is never blocked
    if should_run and total_new > 0:
        def _background_pipeline():
            from app.database import build_engine, build_session_factory
            bg_engine = build_engine()
            bg_session = build_session_factory(bg_engine)()
            try:
                orchestrator.run_pipeline(
                    files=None,
                    auto_qualify=True,
                    auto_draft=True,
                    db=bg_session,
                )
            except Exception as pe:
                logger.warning("Background pipeline execution error: %s", pe)
                orchestrator.add_log(f"[ERROR] Pipeline error: {pe}")
            finally:
                bg_session.close()

        import threading
        worker = threading.Thread(target=_background_pipeline, daemon=True)
        worker.start()
        orchestrator.add_log(
            f"[PIPELINE] Autonomous pipeline initiated in background for {total_new} new leads..."
        )

    # Build response payload
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
        "qualified_count": 0,
        "file_details": file_summaries,
    }
    if rejected:
        result["rejected_files"] = rejected

    if is_json:
        return JSONResponse(result, status_code=200)

    # Redirect for HTML form POST
    msg = (
        f"Successfully processed {files_processed} file(s): "
        f"{total_rows} rows read, {total_new} new contractors added, "
        f"{total_dupes} duplicates merged."
    )
    if rejected:
        msg += f" Rejected: {', '.join(rejected)}"

    return RedirectResponse(url=f"/queue?message={msg.replace(' ', '+')}", status_code=303)


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
