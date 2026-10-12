"""
FS-CAE CLI Entry Point

Usage:
  python -m app.cli import --file data/input/sample_zambian_contractors.csv
  python -m app.cli qualify --limit 20
  python -m app.cli status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.database import build_engine, build_session_factory, Base

# Ensure all models are registered
import app.models  # noqa: F401


def _get_session():
    """Build engine and return a session."""
    settings = get_settings()
    engine = build_engine(settings.DATABASE_URL)
    Base.metadata.create_all(engine)
    SessionFactory = build_session_factory(engine)
    return SessionFactory()


def cmd_import(args):
    """Import businesses from a CSV/XLSX file."""
    from app.services.ingestion.importer import import_file

    session = _get_session()
    filepath = Path(args.file)

    print(f"Importing from: {filepath}")
    summary = import_file(session, filepath, source_type=args.source_type)

    print(f"\n{'='*50}")
    print(f"  Import Summary: {summary.filename}")
    print(f"{'='*50}")
    print(f"  Rows read:            {summary.rows_read}")
    print(f"  Valid records:        {summary.valid_records}")
    print(f"  New businesses:       {summary.new_businesses}")
    print(f"  Duplicates skipped:   {summary.duplicates_skipped}")
    print(f"  Duplicates merged:    {summary.duplicates_merged}")
    print(f"  Invalid/quarantined:  {summary.invalid_quarantined}")
    if summary.errors:
        print(f"  Errors:")
        for err in summary.errors:
            print(f"    - {err}")
    print(f"{'='*50}")
    session.close()


def cmd_qualify(args):
    """Run qualification pipeline on DISCOVERED businesses."""
    from app.services.intelligence.key_manager import GeminiKeyManager
    from app.services.intelligence.gemini_client import GeminiAnalysisClient
    from app.services.intelligence.qualifier import qualify_batch

    settings = get_settings()

    if not settings.GEMINI_API_KEYS:
        print("ERROR: No GEMINI_API_KEYS configured. Set them in .env.")
        sys.exit(1)

    session = _get_session()

    km = GeminiKeyManager(
        keys=settings.GEMINI_API_KEYS,
        rpm_per_key=settings.GEMINI_RPM_PER_KEY,
        cooldown_seconds=settings.GEMINI_COOLDOWN_SECONDS,
    )
    client = GeminiAnalysisClient(km, model_name=settings.GEMINI_MODEL)

    print(f"Qualifying businesses (limit={args.limit or 'all'})...")
    summary = qualify_batch(session, client, limit=args.limit)

    print(f"\n{'='*50}")
    print(f"  Qualification Summary")
    print(f"{'='*50}")
    print(f"  Processed:     {summary.total_processed}")
    print(f"  Qualified:     {summary.qualified}")
    print(f"  Disqualified:  {summary.disqualified}")
    print(f"  Errors:        {summary.errors}")
    if summary.error_details:
        for err in summary.error_details:
            print(f"    - {err}")
    print(f"{'='*50}")
    session.close()


def cmd_status(args):
    """Print table of total businesses by qualification state."""
    from app.models.prospect import ProspectQualification
    from sqlalchemy import func

    session = _get_session()

    results = (
        session.query(
            ProspectQualification.qualification_state,
            func.count(ProspectQualification.id),
        )
        .group_by(ProspectQualification.qualification_state)
        .all()
    )

    from app.models.business import Business
    total_biz = session.query(func.count(Business.id)).scalar()

    print(f"\n{'='*50}")
    print(f"  FS-CAE Pipeline Status")
    print(f"{'='*50}")
    print(f"  Total Businesses: {total_biz}")
    print(f"  {'─'*46}")

    if results:
        for state, count in sorted(results, key=lambda x: x[0]):
            print(f"  {state:<30} {count:>5}")
    else:
        print("  No prospect qualifications found.")

    print(f"{'='*50}")
    session.close()


def cmd_outreach_draft(args):
    """Draft outreach copy for QUALIFIED prospects."""
    from app.services.outreach.batch_manager import draft_outreach_for_qualified

    session = _get_session()
    limit = args.limit or 20
    print(f"Drafting outreach for qualified prospects (limit={limit})...")
    jobs = draft_outreach_for_qualified(session, limit=limit)

    print(f"\n{'='*50}")
    print(f"  Outreach Draft Summary")
    print(f"{'='*50}")
    print(f"  Drafted jobs: {len(jobs)}")
    for j in jobs:
        print(f"  - [{j.id[:8]}] {j.channel:<12} -> {j.recipient_target}")
    print(f"{'='*50}")
    session.close()


def cmd_outreach_approve(args):
    """Approve drafted outreach jobs to QUEUED."""
    from app.services.outreach.batch_manager import approve_batch

    session = _get_session()
    job_ids = None if args.all else (args.job_ids or [])
    jobs = approve_batch(session, job_ids=job_ids, channel=args.channel)

    print(f"\n{'='*50}")
    print(f"  Batch Approval Summary")
    print(f"{'='*50}")
    print(f"  Approved to QUEUED: {len(jobs)}")
    for j in jobs:
        print(f"  - [{j.id[:8]}] {j.channel:<12} -> {j.recipient_target}")
    print(f"{'='*50}")
    session.close()


def cmd_outreach_reject(args):
    """Reject outreach jobs and mark BLOCKED with reason."""
    from app.services.outreach.batch_manager import reject_batch

    session = _get_session()
    jobs = reject_batch(session, job_ids=args.job_ids, reason=args.reason)

    print(f"\n{'='*50}")
    print(f"  Batch Rejection Summary")
    print(f"{'='*50}")
    print(f"  Rejected to BLOCKED: {len(jobs)}")
    for j in jobs:
        print(f"  - [{j.id[:8]}] Reason: {j.blocked_reason}")
    print(f"{'='*50}")
    session.close()


def cmd_outreach_dispatch(args):
    """Execute dispatch queue worker."""
    from app.services.outreach.dispatcher import DispatchWorker

    session = _get_session()
    worker = DispatchWorker()
    channel = args.channel.upper() if args.channel else None

    print(f"Executing dispatcher (channel={channel or 'ALL'}, dry_run={args.dry_run}, force={args.force})...")
    summary = worker.dispatch_queue(
        session,
        limit=args.limit,
        channel=channel,
        force=args.force,
        dry_run=args.dry_run,
    )

    print(f"\n{'='*50}")
    print(f"  Dispatch Execution Summary")
    print(f"{'='*50}")
    print(f"  Total Processed:  {summary.total_processed}")
    print(f"  Dispatched:       {summary.dispatched}")
    print(f"  Blocked:          {summary.blocked}")
    print(f"  Failed:           {summary.failed}")
    if summary.held_by_operating_hours:
        print(f"  Status:           HELD (outside 08:00–16:30 CAT Mon-Fri; use --force to override)")
    if summary.daily_cap_reached:
        print(f"  Status:           STOPPED (daily cap reached)")
    print(f"{'='*50}")
    session.close()


def cmd_conversation_reply(args):
    """Simulate an inbound prospect message."""
    from app.services.conversation.conversation_service import ConversationService

    session = _get_session()
    service = ConversationService()
    res = service.handle_inbound_message(
        session,
        business_id=args.business_id,
        text=args.text,
        channel=args.channel or "FB_MESSENGER",
    )

    print(f"\n{'='*50}")
    print(f"  Inbound Reply Processed")
    print(f"{'='*50}")
    print(f"  Conversation ID:  {res['conversation_id']}")
    print(f"  Business ID:      {res['business_id']}")
    print(f"  Detected Intent:  {res['intent']}")
    print(f"  State Transition: {res['old_state']} -> {res['new_state']}")
    if res.get("audit_id"):
        print(f"  Generated Audit:  {res['audit_id']}")
    if res.get("suggested_reply"):
        print(f"\n  --- Response Draft ---\n{res['suggested_reply']}")
    print(f"{'='*50}")
    session.close()


def cmd_conversation_list(args):
    """List conversations and their current states."""
    from app.models.business import Business
    from app.models.conversation import Conversation

    session = _get_session()
    convs = (
        session.query(Conversation, Business)
        .join(Business, Conversation.business_id == Business.id)
        .order_by(Conversation.last_activity_at.desc())
        .limit(args.limit or 50)
        .all()
    )

    print(f"\n{'='*65}")
    print(f"  Active Conversations ({len(convs)})")
    print(f"{'='*65}")
    for conv, biz in convs:
        print(f"  [{conv.id[:8]}] {biz.company_name:<30} | {conv.state:<22} | {conv.channel}")
    print(f"{'='*65}")
    session.close()


def cmd_audit_generate(args):
    """Generate 3-point capability audit for a business."""
    from app.models.business import Business
    from app.services.commercial.audit_generator import (
        format_audit_markdown,
        generate_commercial_audit,
    )

    session = _get_session()
    biz = session.query(Business).filter(Business.id == args.business_id).one()
    audit = generate_commercial_audit(session, args.business_id)

    print(f"\n{'='*50}")
    print(f"  3-Point Audit Generated: {audit.id}")
    print(f"{'='*50}")
    print(format_audit_markdown(audit, biz))
    print(f"{'='*50}")
    session.close()


def cmd_proposal_create(args):
    """Create Grand Slam proposal for a business."""
    from app.models.business import Business
    from app.schemas.states import PackageType
    from app.services.commercial.proposal_generator import (
        create_grand_slam_proposal,
        format_proposal_markdown,
        issue_proposal,
    )

    session = _get_session()
    pkg_type = (
        PackageType.LAUNCH_SYSTEM_BASIC.value
        if (args.package or "").lower() == "basic"
        else PackageType.CAPABILITY_PORTAL_CORE.value
    )
    biz = session.query(Business).filter(Business.id == args.business_id).one()
    proposal = create_grand_slam_proposal(session, args.business_id, package_type=pkg_type)
    if args.issue:
        proposal = issue_proposal(session, proposal.id)

    print(f"\n{'='*50}")
    print(f"  Grand Slam Proposal Created: {proposal.id} (Status: {proposal.status})")
    print(f"{'='*50}")
    print(format_proposal_markdown(proposal, biz))
    print(f"{'='*50}")
    session.close()


def cmd_invoice_create(args):
    """Create milestone invoice for a proposal."""
    from app.models.business import Business
    from app.models.commercial import Proposal
    from app.schemas.states import MilestoneTier
    from app.services.commercial.invoice_service import (
        create_milestone_invoice,
        format_invoice_markdown,
    )

    session = _get_session()
    tier_map = {
        "mobilization": MilestoneTier.MOBILIZATION.value,
        "mid_project": MilestoneTier.MID_PROJECT.value,
        "pre_launch": MilestoneTier.PRE_LAUNCH.value,
    }
    tier = tier_map.get((args.tier or "mobilization").lower(), MilestoneTier.MOBILIZATION.value)

    invoice = create_milestone_invoice(session, args.proposal_id, milestone_tier=tier)
    proposal = session.query(Proposal).filter(Proposal.id == args.proposal_id).one()
    biz = proposal.business

    print(f"\n{'='*50}")
    print(f"  Milestone Invoice Generated: {invoice.invoice_number}")
    print(f"{'='*50}")
    print(format_invoice_markdown(invoice, proposal, biz))
    print(f"{'='*50}")
    session.close()


def cmd_payment_verify(args):
    """Reconcile and verify payment for an invoice."""
    from app.models.commercial import Invoice
    from app.services.commercial.payment_service import reconcile_payment

    session = _get_session()
    event = reconcile_payment(
        session,
        invoice_id=args.invoice_id,
        amount_received=float(args.amount),
        transaction_reference=args.ref,
        verified_by=args.by or "Operator",
        notes=args.notes,
    )
    invoice = session.query(Invoice).filter(Invoice.id == args.invoice_id).one()

    print(f"\n{'='*50}")
    print(f"  Payment Reconciliation Succeeded")
    print(f"{'='*50}")
    print(f"  Payment Event ID: {event.id}")
    print(f"  Invoice Number:   {invoice.invoice_number}")
    print(f"  Invoice Status:   {invoice.status}")
    print(f"  Amount Reconciled: K{float(event.amount_received):,.2f}")
    print(f"  Transaction Ref:  {event.transaction_reference}")
    print(f"  Verified By:      {event.verified_by}")
    print(f"{'='*50}")
    session.close()


def cmd_project_brief(args):
    """Print Builder.io implementation brief for an activated client."""
    from app.services.commercial.activation_service import (
        compile_delivery_brief,
        format_delivery_brief_markdown,
    )

    session = _get_session()
    brief = compile_delivery_brief(session, args.business_id)

    print(f"\n{'='*50}")
    print(f"  Builder.io Implementation Brief")
    print(f"{'='*50}")
    print(format_delivery_brief_markdown(brief))
    print(f"{'='*50}")
    session.close()


def cmd_followup_run(args):
    """Evaluate stalled conversations and queue context-aware follow-ups."""
    from app.services.outreach.followup_service import evaluate_stalled_conversations_and_queue_followups

    session = _get_session()
    queued = evaluate_stalled_conversations_and_queue_followups(session)

    print(f"\n{'='*50}")
    print(f"  Follow-Up Evaluation Summary")
    print(f"{'='*50}")
    print(f"  Queued Follow-Ups: {len(queued)}")
    for j in queued:
        print(f"  - [{j.id[:8]}] Key: {j.idempotency_key:<35} -> {j.recipient_target}")
    print(f"{'='*50}")
    session.close()


def cmd_verify_gemini(args):
    """Run live multi-key Gemini verification and health-check."""
    from app.services.intelligence.verifier import run_gemini_live_verification
    success = run_gemini_live_verification()
    if not success:
        sys.exit(1)


def cmd_web(args):
    """Launch Operator Control Tower Web Interface."""
    import uvicorn

    print(f"\n=================================================================")
    print(f"  FOUNDRY SYSTEMS — OPERATOR CONTROL TOWER")
    print(f"  Listening on http://{args.host}:{args.port}")
    print(f"=================================================================\n")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


def main():
    parser = argparse.ArgumentParser(
        prog="fs-cae",
        description="Foundry Systems Client Acquisition Engine CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # web
    p_web = subparsers.add_parser("web", help="Launch Operator Control Tower Web Interface")
    p_web.add_argument("--host", default="127.0.0.1", help="Host binding (default: 127.0.0.1)")
    p_web.add_argument("--port", "-p", type=int, default=8000, help="Port binding (default: 8000)")
    p_web.add_argument("--reload", action="store_true", default=True, help="Enable auto-reload for development (default: True)")
    p_web.add_argument("--no-reload", dest="reload", action="store_false", help="Disable auto-reload")
    p_web.set_defaults(func=cmd_web)

    # verify-gemini
    p_verify = subparsers.add_parser("verify-gemini", help="Validate and live-test configured Gemini API keys")
    p_verify.set_defaults(func=cmd_verify_gemini)

    # import
    p_import = subparsers.add_parser("import", help="Import businesses from file")
    p_import.add_argument("--file", "-f", required=True, help="Path to CSV or XLSX file")
    p_import.add_argument(
        "--source-type", default="INSTANT_DATA_SCRAPER",
        help="Source type label (default: INSTANT_DATA_SCRAPER)",
    )
    p_import.set_defaults(func=cmd_import)

    # qualify
    p_qualify = subparsers.add_parser("qualify", help="Run qualification pipeline")
    p_qualify.add_argument("--limit", "-l", type=int, default=None, help="Max businesses to process")
    p_qualify.set_defaults(func=cmd_qualify)

    # status
    p_status = subparsers.add_parser("status", help="Show pipeline status")
    p_status.set_defaults(func=cmd_status)

    # outreach
    p_outreach = subparsers.add_parser("outreach", help="Outreach batch and dispatch commands")
    outreach_subparsers = p_outreach.add_subparsers(dest="outreach_command", help="Outreach subcommands")

    # outreach draft
    p_draft = outreach_subparsers.add_parser("draft", help="Draft outreach for qualified prospects")
    p_draft.add_argument("--limit", "-l", type=int, default=20, help="Max prospects to draft")
    p_draft.set_defaults(func=cmd_outreach_draft)

    # outreach approve
    p_approve = outreach_subparsers.add_parser("approve", help="Approve drafted jobs into queue")
    p_approve.add_argument("--all", action="store_true", help="Approve all drafted jobs")
    p_approve.add_argument("--job-ids", nargs="+", help="Specific job IDs to approve")
    p_approve.add_argument("--channel", help="Override channel (FB_MESSENGER, EMAIL, MANUAL)")
    p_approve.set_defaults(func=cmd_outreach_approve)

    # outreach reject
    p_reject = outreach_subparsers.add_parser("reject", help="Reject jobs with a reason")
    p_reject.add_argument("--job-ids", nargs="+", required=True, help="Specific job IDs to reject")
    p_reject.add_argument("--reason", "-r", required=True, help="Reason for rejection")
    p_reject.set_defaults(func=cmd_outreach_reject)

    # outreach dispatch
    p_dispatch = outreach_subparsers.add_parser("dispatch", help="Execute queue dispatch worker")
    p_dispatch.add_argument("--dry-run", action="store_true", help="Simulate dispatch without sending")
    p_dispatch.add_argument("--channel", help="Filter by channel (FB_MESSENGER, EMAIL, assisted, MANUAL)")
    p_dispatch.add_argument("--force", action="store_true", help="Bypass operating hours restriction")
    p_dispatch.add_argument("--limit", type=int, default=None, help="Max jobs to dispatch")
    p_dispatch.set_defaults(func=cmd_outreach_dispatch)

    # conversation
    p_conv = subparsers.add_parser("conversation", help="Conversation management")
    conv_subparsers = p_conv.add_subparsers(dest="conv_command", help="Conversation subcommands")

    # conversation reply
    p_conv_reply = conv_subparsers.add_parser("reply", help="Simulate inbound reply")
    p_conv_reply.add_argument("--business-id", "-b", required=True, help="Business ID")
    p_conv_reply.add_argument("--text", "-t", required=True, help="Inbound message text")
    p_conv_reply.add_argument("--channel", default="FB_MESSENGER", help="Channel (default: FB_MESSENGER)")
    p_conv_reply.set_defaults(func=cmd_conversation_reply)

    # conversation list
    p_conv_list = conv_subparsers.add_parser("list", help="List active conversations")
    p_conv_list.add_argument("--limit", "-l", type=int, default=50, help="Max conversations to list")
    p_conv_list.set_defaults(func=cmd_conversation_list)

    # audit
    p_audit = subparsers.add_parser("audit", help="Commercial audit commands")
    audit_subparsers = p_audit.add_subparsers(dest="audit_command", help="Audit subcommands")

    p_audit_gen = audit_subparsers.add_parser("generate", help="Generate 3-point capability audit")
    p_audit_gen.add_argument("--business-id", "-b", required=True, help="Business ID")
    p_audit_gen.set_defaults(func=cmd_audit_generate)

    # proposal
    p_prop = subparsers.add_parser("proposal", help="Grand Slam proposal commands")
    prop_subparsers = p_prop.add_subparsers(dest="prop_command", help="Proposal subcommands")

    p_prop_create = prop_subparsers.add_parser("create", help="Create Grand Slam proposal")
    p_prop_create.add_argument("--business-id", "-b", required=True, help="Business ID")
    p_prop_create.add_argument("--package", "-p", default="core", help="Package type: core or basic")
    p_prop_create.add_argument("--issue", action="store_true", help="Formally issue proposal")
    p_prop_create.set_defaults(func=cmd_proposal_create)

    # invoice
    p_inv = subparsers.add_parser("invoice", help="Invoice management commands")
    inv_subparsers = p_inv.add_subparsers(dest="inv_command", help="Invoice subcommands")

    p_inv_create = inv_subparsers.add_parser("create", help="Create milestone invoice")
    p_inv_create.add_argument("--proposal-id", "-p", required=True, help="Proposal ID")
    p_inv_create.add_argument("--tier", "-t", default="mobilization", help="Milestone tier: mobilization, mid_project, pre_launch")
    p_inv_create.set_defaults(func=cmd_invoice_create)

    # payment
    p_pay = subparsers.add_parser("payment", help="Payment reconciliation commands")
    pay_subparsers = p_pay.add_subparsers(dest="pay_command", help="Payment subcommands")

    p_pay_verify = pay_subparsers.add_parser("verify", help="Verify and reconcile invoice payment")
    p_pay_verify.add_argument("--invoice-id", "-i", required=True, help="Invoice ID")
    p_pay_verify.add_argument("--amount", "-a", type=float, required=True, help="Amount received (ZMW)")
    p_pay_verify.add_argument("--ref", "-r", required=True, help="Transaction reference")
    p_pay_verify.add_argument("--by", default="Founder", help="Operator identifier")
    p_pay_verify.add_argument("--notes", help="Optional payment notes")
    p_pay_verify.set_defaults(func=cmd_payment_verify)

    # project
    p_proj = subparsers.add_parser("project", help="Project and activation commands")
    proj_subparsers = p_proj.add_subparsers(dest="proj_command", help="Project subcommands")

    p_proj_brief = proj_subparsers.add_parser("brief", help="Compile Builder.io implementation brief")
    p_proj_brief.add_argument("--business-id", "-b", required=True, help="Business ID")
    p_proj_brief.set_defaults(func=cmd_project_brief)

    # followup
    p_followup = subparsers.add_parser("followup", help="Follow-up management commands")
    followup_subparsers = p_followup.add_subparsers(dest="followup_command", help="Followup subcommands")

    p_followup_run = followup_subparsers.add_parser("run", help="Evaluate stalled conversations and queue follow-ups")
    p_followup_run.set_defaults(func=cmd_followup_run)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "outreach" and not getattr(args, "outreach_command", None):
        p_outreach.print_help()
        sys.exit(1)

    if args.command == "conversation" and not getattr(args, "conv_command", None):
        p_conv.print_help()
        sys.exit(1)

    if args.command == "audit" and not getattr(args, "audit_command", None):
        p_audit.print_help()
        sys.exit(1)

    if args.command == "proposal" and not getattr(args, "prop_command", None):
        p_prop.print_help()
        sys.exit(1)

    if args.command == "invoice" and not getattr(args, "inv_command", None):
        p_inv.print_help()
        sys.exit(1)

    if args.command == "payment" and not getattr(args, "pay_command", None):
        p_pay.print_help()
        sys.exit(1)

    if args.command == "project" and not getattr(args, "proj_command", None):
        p_proj.print_help()
        sys.exit(1)

    if args.command == "followup" and not getattr(args, "followup_command", None):
        p_followup.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
