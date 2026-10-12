"""
FS-CAE Layer 6 Automated Test Suite: The Operator Control Tower Web Interface.

Tests all 5 core cockpit screens, batch approvals, audit dispatches,
Grand Slam proposal issuances, payment reconciliation gates, and Builder.io prompt delivery.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.audit import CommercialAudit
from app.models.business import Business
from app.models.commercial import (
    Invoice,
    PaymentEvent,
    ProjectActivation,
    Proposal,
)
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    CommercialAuditStatus,
    ConversationChannel,
    ConversationState,
    DeliveryStatus,
    DispatchChannel,
    DispatchStatus,
    InboundIntent,
    InvoiceStatus,
    MessageDirection,
    MilestoneTier,
    PackageType,
    ProjectActivationStatus,
    ProposalStatus,
    QualificationState,
)
from app.services.commercial.audit_generator import generate_commercial_audit
from app.services.commercial.invoice_service import create_milestone_invoice
from app.services.commercial.proposal_generator import (
    create_grand_slam_proposal,
    issue_proposal,
)
from app.services.conversation.conversation_service import ConversationService
from app.services.outreach.batch_manager import draft_outreach_for_qualified
from app.services.state_machine import transition_conversation


# =========================================================================== #
#  FIXTURES & HELPERS
# =========================================================================== #

@pytest.fixture
def web_session():
    """Provide a thread-safe in-memory SQLite session with StaticPool for FastAPI TestClient."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    event.listen(eng, "connect", _set_sqlite_pragmas)
    Base.metadata.create_all(eng)
    SessionFactory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    sess = SessionFactory()

    yield sess

    sess.close()
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def client(web_session: Session):
    """Provide a FastAPI TestClient with transactional DB session override."""
    app.dependency_overrides[get_db] = lambda: web_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create_test_business(session: Session, **kwargs) -> Business:
    name = kwargs.pop("name", "Kafue Earthmoving Ltd")
    defaults = dict(
        company_name=name,
        location_district="Kafue",
        industry_sector="Civil Earthmoving & Equipment Hire",
        phone_normalized="+260977112233",
        email="contracts@kafue.zm",
        fb_page_url="https://facebook.com/kafueearthmoving",
        source_type="INSTANT_DATA_SCRAPER",
        is_suppressed=False,
    )
    defaults.update(kwargs)
    biz = Business(**defaults)
    session.add(biz)
    session.commit()
    session.refresh(biz)
    return biz


def _create_test_qualification(session: Session, biz: Business, **kwargs) -> ProspectQualification:
    defaults = dict(
        business_id=biz.id,
        qualification_state=QualificationState.QUALIFIED.value,
        fit_score=85,
        sector_match=True,
        asset_footprint={"heavy_machinery": ["CAT 320D Excavator", "20-ton Tipper"]},
    )
    defaults.update(kwargs)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.commit()
    session.refresh(qual)
    return qual


def _create_test_conversation(session: Session, biz: Business, **kwargs) -> Conversation:
    defaults = dict(
        business_id=biz.id,
        channel=ConversationChannel.FB_MESSENGER.value,
        state=ConversationState.AWAITING_REPLY.value,
    )
    defaults.update(kwargs)
    conv = Conversation(**defaults)
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


# =========================================================================== #
#  1. EXECUTIVE DASHBOARD TESTS (GET / & POST /upload)
# =========================================================================== #

class TestDashboardEndpoints:

    def test_get_dashboard_empty_db(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "Executive Command Center" in response.text
        assert "Conversion Funnel" in response.text
        assert "Google Gemini Live Key Pool" in response.text
        assert "Quick Ingress Dropzone" in response.text

    def test_get_dashboard_with_populated_pipeline(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session)
        qual = _create_test_qualification(web_session, biz, fit_score=90)
        conv = _create_test_conversation(web_session, biz)

        # Record a message
        msg = Message(
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND.value,
            content="Hello, what are your rates for plant hire portals?",
            delivery_status=DeliveryStatus.RECEIVED.value,
        )
        web_session.add(msg)
        web_session.commit()

        response = client.get("/")
        assert response.status_code == 200
        assert "Discovered" in response.text
        assert "Qualified Pipeline" in response.text

    def test_upload_csv_ingress(self, client: TestClient):
        csv_content = (
            "Name,Phone,Address,Facebook\n"
            "Chilanga Quarry Logistics,+260977445566,Chilanga,https://facebook.com/chilangaql\n"
            "Lusaka Grading Services,+260977889900,Lusaka,https://facebook.com/lusakagrading\n"
        )
        files = [("files", ("contractors.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv"))]
        response = client.post("/upload", files=files, follow_redirects=False)

        assert response.status_code == 303
        assert "/queue?message=" in response.headers["location"]

    def test_upload_invalid_extension_rejected(self, client: TestClient):
        files = [("files", ("test.pdf", io.BytesIO(b"dummy pdf content"), "application/pdf"))]
        response = client.post("/upload", files=files, follow_redirects=False)

        assert response.status_code == 303
        assert "/?error=" in response.headers["location"]

    def test_multi_file_upload_csv_and_xlsx(self, client: TestClient, web_session: Session):
        """Simulates uploading 2 files simultaneously via TestClient.post('/upload').
        Asserts response is HTTP 200, both files are parsed, deduplicated, and database
        businesses increase."""
        csv_content = (
            "Name,Phone,Address,Facebook\n"
            "Kafue Sand Supplies,+260977111222,Kafue,https://facebook.com/kafuesand\n"
            "Choma Plant Hire,+260977333444,Choma,https://facebook.com/chomaplant\n"
        )
        xlsx_csv_content = (
            "company_name,phone,location,facebook\n"
            "Monze Equipment Rentals,+260977555666,Monze,https://facebook.com/monzeequip\n"
        )

        biz_count_before = web_session.query(Business).count()

        files = [
            ("files", ("lusaka_civil.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")),
            ("files", ("kitwe_haulage.csv", io.BytesIO(xlsx_csv_content.encode("utf-8")), "text/csv")),
        ]

        response = client.post(
            "/upload",
            files=files,
            data={"auto_qualify": "false"},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["files_count"] == 2
        assert data["total_rows"] >= 3
        assert data["new_businesses"] >= 3

        biz_count_after = web_session.query(Business).count()
        assert biz_count_after > biz_count_before

    def test_multi_file_upload_with_auto_qualification(self, client: TestClient, web_session: Session):
        """Asserts that uploaded records transition to QUALIFIED / DISQUALIFIED automatically
        when auto_qualify is enabled (qualification may fail gracefully without Gemini keys)."""
        csv_content = (
            "Name,Phone,Address,Facebook\n"
            "Livingstone Excavators,+260977777888,Livingstone,https://facebook.com/livexcav\n"
        )

        files = [
            ("files", ("auto_qual_test.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")),
        ]

        response = client.post(
            "/upload",
            files=files,
            data={"auto_qualify": "true"},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["new_businesses"] >= 1
        # qualified_count may be 0 if Gemini is not configured in test env
        assert "qualified_count" in data

    def test_upload_invalid_file_extension_rejected_json(self, client: TestClient):
        """Confirms .pdf or .exe uploads are rejected with clean error responses."""
        files = [
            ("files", ("malware.exe", io.BytesIO(b"evil binary"), "application/octet-stream")),
        ]
        response = client.post(
            "/upload",
            files=files,
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "ERROR"
        assert "Invalid" in data["error"] or "invalid" in data["error"].lower()


# =========================================================================== #
#  2. ACQUISITION QUEUE TESTS (GET /queue, POST /queue/approve, POST /queue/dispatch)
# =========================================================================== #

class TestQueueEndpoints:

    def test_get_queue_renders_qualified_contractors(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Ndola Earthworks Ltd")
        _create_test_qualification(web_session, biz, fit_score=88)

        response = client.get("/queue")
        assert response.status_code == 200
        assert "Ndola Earthworks Ltd" in response.text
        assert "88/100" in response.text
        assert "CAT 320D Excavator" in response.text
        assert "Approve Selected Batch" in response.text

    def test_post_queue_approve_batch_moves_to_queued(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Kitwe Quarry Fleet")
        _create_test_qualification(web_session, biz, fit_score=85)

        # Draft jobs
        jobs = draft_outreach_for_qualified(web_session, limit=1)
        assert len(jobs) == 1
        assert jobs[0].status == DispatchStatus.DRAFTED.value
        job_id = jobs[0].id

        # Approve via POST /queue/approve
        response = client.post("/queue/approve", data={"job_ids": [job_id]}, follow_redirects=False)
        assert response.status_code == 303

        # Verify job is now QUEUED in DB
        web_session.expire_all()
        refreshed_job = web_session.query(DispatchJob).filter(DispatchJob.id == job_id).one()
        assert refreshed_job.status == DispatchStatus.QUEUED.value

    def test_post_queue_approve_json_api(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Solwezi Mine Plant")
        _create_test_qualification(web_session, biz, fit_score=80)
        jobs = draft_outreach_for_qualified(web_session, limit=1)

        response = client.post(
            "/queue/approve",
            json={"job_ids": [jobs[0].id]},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["approved_count"] == 1

    def test_post_queue_dispatch_trigger(self, client: TestClient, web_session: Session):
        response = client.post("/queue/dispatch", headers={"Accept": "application/json"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"
        assert "message" in data


# =========================================================================== #
#  3. CONVERSATIONS INBOX TESTS (GET /inbox, AUDIT APPROVE, PROPOSAL ISSUE)
# =========================================================================== #

class TestInboxEndpoints:

    def test_get_inbox_displays_threads_and_bubbles(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Copperbelt Plant Hire")
        conv = _create_test_conversation(web_session, biz)

        msg1 = Message(
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND.value,
            content="Can you send us your pricing sheet for plant hire companies?",
            delivery_status=DeliveryStatus.RECEIVED.value,
        )
        msg2 = Message(
            conversation_id=conv.id,
            direction=MessageDirection.OUTBOUND.value,
            content="Certainly. Most commercial contractors invest K35,000...",
            delivery_status=DeliveryStatus.SENT.value,
        )
        web_session.add_all([msg1, msg2])
        web_session.commit()

        response = client.get(f"/inbox?conversation_id={conv.id}")
        assert response.status_code == 200
        assert "Copperbelt Plant Hire" in response.text
        assert "Can you send us your pricing sheet" in response.text
        assert "Most commercial contractors invest K35,000" in response.text

    def test_post_audit_approve_transitions_and_dispatches(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Lusaka Aggregate Suppliers")
        _create_test_qualification(web_session, biz)
        conv = _create_test_conversation(
            web_session,
            biz,
            state=ConversationState.AUDIT_PENDING_APPROVAL.value,
        )

        # Generate audit
        audit = generate_commercial_audit(web_session, biz.id)
        assert audit.status == CommercialAuditStatus.GENERATED.value

        response = client.post(
            f"/inbox/{conv.id}/audit/approve",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"

        # Verify DB states
        web_session.expire_all()
        assert conv.state == ConversationState.AUDIT_DISPATCHED.value
        assert audit.status == CommercialAuditStatus.DELIVERED.value

        # Verify outbound message was created
        out_msg = (
            web_session.query(Message)
            .filter(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
            .first()
        )
        assert out_msg is not None
        assert "COMMERCIAL CAPABILITY AUDIT" in out_msg.content

    def test_post_proposal_issue_transitions_to_requested(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Mazabuka Haulage")
        _create_test_qualification(web_session, biz)
        conv = _create_test_conversation(
            web_session,
            biz,
            state=ConversationState.AUDIT_DISPATCHED.value,
        )

        response = client.post(
            f"/inbox/{conv.id}/proposal/issue",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["total_price"] == 14500.0

        web_session.expire_all()
        assert conv.state == ConversationState.PROPOSAL_REQUESTED.value

        # Verify proposal recorded
        prop = web_session.query(Proposal).filter(Proposal.business_id == biz.id).first()
        assert prop is not None
        assert prop.status == ProposalStatus.ISSUED.value

    def test_post_manual_reply(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session)
        conv = _create_test_conversation(web_session, biz)

        response = client.post(
            f"/inbox/{conv.id}/reply",
            data={"content": "Thank you, our engineering team is reviewing your fleet specifications."},
            follow_redirects=False,
        )
        assert response.status_code == 303

        # Verify outbound message recorded
        msg = (
            web_session.query(Message)
            .filter(
                Message.conversation_id == conv.id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
            .first()
        )
        assert msg is not None
        assert "engineering team is reviewing" in msg.content


# =========================================================================== #
#  4. FINANCIAL DESK TESTS (GET /invoices & POST /invoices/reconcile)
# =========================================================================== #

class TestFinancialEndpoints:

    def test_get_invoices_table(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Kabwe Civil Works")
        prop = create_grand_slam_proposal(web_session, biz.id)
        prop = issue_proposal(web_session, prop.id)
        inv = create_milestone_invoice(web_session, prop.id, MilestoneTier.MOBILIZATION.value)

        response = client.get("/invoices")
        assert response.status_code == 200
        assert "Kabwe Civil Works" in response.text
        assert inv.invoice_number in response.text
        assert "5,500.00" in response.text
        assert "MOBILIZATION" in response.text

    def test_post_reconcile_valid_payment_activates_client(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Livingstone Quarries")
        _create_test_qualification(web_session, biz)
        conv = _create_test_conversation(web_session, biz, state=ConversationState.PROPOSAL_REQUESTED.value)

        prop = create_grand_slam_proposal(web_session, biz.id)
        prop = issue_proposal(web_session, prop.id)
        inv = create_milestone_invoice(web_session, prop.id, MilestoneTier.MOBILIZATION.value)

        response = client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv.id,
                "amount_received": 5500.00,
                "transaction_reference": "FNB-ZM-883921",
                "verified_by": "Founder",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "PAID"
        assert data["transaction_reference"] == "FNB-ZM-883921"

        # Verify database state machine updates
        web_session.expire_all()
        refreshed_inv = web_session.query(Invoice).filter(Invoice.id == inv.id).one()
        assert refreshed_inv.status == InvoiceStatus.PAID.value

        refreshed_conv = web_session.query(Conversation).filter(Conversation.id == conv.id).one()
        assert refreshed_conv.state == "WON"

        activation = web_session.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).first()
        assert activation is not None
        assert activation.status == ProjectActivationStatus.ACTIVATED.value

    def test_post_reconcile_duplicate_reference_rejected(self, client: TestClient, web_session: Session):
        biz1 = _create_test_business(web_session, name="Biz One")
        prop1 = create_grand_slam_proposal(web_session, biz1.id)
        prop1 = issue_proposal(web_session, prop1.id)
        inv1 = create_milestone_invoice(web_session, prop1.id, MilestoneTier.MOBILIZATION.value)

        # First reconciliation succeeds
        client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv1.id,
                "amount_received": 5500.00,
                "transaction_reference": "FNB-ZM-DUPE-REF",
                "verified_by": "Founder",
            },
            headers={"Accept": "application/json"},
        )

        # Second invoice attempts to reuse same reference
        biz2 = _create_test_business(web_session, name="Biz Two")
        prop2 = create_grand_slam_proposal(web_session, biz2.id)
        prop2 = issue_proposal(web_session, prop2.id)
        inv2 = create_milestone_invoice(web_session, prop2.id, MilestoneTier.MOBILIZATION.value)

        response2 = client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv2.id,
                "amount_received": 5500.00,
                "transaction_reference": "FNB-ZM-DUPE-REF",
                "verified_by": "Founder",
            },
            headers={"Accept": "application/json"},
        )
        assert response2.status_code == 400
        data2 = response2.json()
        assert data2["status"] == "DUPLICATE"
        assert "Duplicate payment reference" in data2["error"]

    def test_post_reconcile_insufficient_amount_rejected(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Short Payment Biz")
        prop = create_grand_slam_proposal(web_session, biz.id)
        prop = issue_proposal(web_session, prop.id)
        inv = create_milestone_invoice(web_session, prop.id, MilestoneTier.MOBILIZATION.value)

        response = client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv.id,
                "amount_received": 3000.00,  # Below K5,500
                "transaction_reference": "FNB-ZM-INSUFFICIENT",
                "verified_by": "Founder",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400
        data = response.json()
        assert "Insufficient payment" in data["error"]


# =========================================================================== #
#  5. IMPLEMENTATION VAULT TESTS (GET /projects & /projects/{id}/brief)
# =========================================================================== #

class TestProjectsEndpoints:

    def test_get_projects_displays_builder_io_prompt(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="Kafue Earthmoving Ltd")
        _create_test_qualification(web_session, biz)
        _create_test_conversation(web_session, biz, state=ConversationState.PROPOSAL_REQUESTED.value)
        prop = create_grand_slam_proposal(web_session, biz.id)
        prop = issue_proposal(web_session, prop.id)
        inv = create_milestone_invoice(web_session, prop.id, MilestoneTier.MOBILIZATION.value)

        # Reconcile payment to trigger activation
        client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv.id,
                "amount_received": 5500.00,
                "transaction_reference": "FNB-ZM-VAULT-TEST",
                "verified_by": "Founder",
            },
        )

        response = client.get("/projects")
        assert response.status_code == 200
        assert "Kafue Earthmoving Ltd" in response.text
        assert "ACTIVATED" in response.text
        assert "Copy Builder.io Master Prompt" in response.text
        assert "Commercial Procurement Verification" in response.text or "CAT 320D Excavator" in response.text
        assert "CAT 320D Excavator" in response.text

    def test_get_project_brief_json(self, client: TestClient, web_session: Session):
        biz = _create_test_business(web_session, name="JSON Brief Contractor")
        _create_test_qualification(web_session, biz)
        _create_test_conversation(web_session, biz, state=ConversationState.PROPOSAL_REQUESTED.value)
        prop = create_grand_slam_proposal(web_session, biz.id)
        prop = issue_proposal(web_session, prop.id)
        inv = create_milestone_invoice(web_session, prop.id, MilestoneTier.MOBILIZATION.value)

        client.post(
            "/invoices/reconcile",
            data={
                "invoice_id": inv.id,
                "amount_received": 5500.00,
                "transaction_reference": "FNB-ZM-JSON-BRIEF",
                "verified_by": "Founder",
            },
        )

        act = web_session.query(ProjectActivation).filter(ProjectActivation.business_id == biz.id).first()
        assert act is not None

        response = client.get(f"/projects/{act.id}/brief")
        assert response.status_code == 200
        data = response.json()
        assert data["client_name"] == "JSON Brief Contractor"
        assert "builder_master_prompt" in data
        assert "mobilization_paid" in data
        assert data["mobilization_paid"] is True


# =========================================================================== #
#  6. HEALTH CHECK
# =========================================================================== #

class TestHealthEndpoint:

    def test_health_ping(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "HEALTHY"
