"""
Tests for FS-CAE Autonomous Pipeline Orchestrator & Live Telemetry.

Covers:
  1. Full chain-reaction workflow: DISCOVERED -> EVALUATING -> QUALIFIED -> STAGED_FOR_OUTREACH
  2. DispatchJob creation in DRAFTED status with $100M Leads copy
  3. Real-time telemetry streaming endpoint (/api/pipeline/telemetry)
  4. Master 1-click execution endpoint (/api/pipeline/run-all)
  5. Heuristic fallback analyzer resilience
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import DispatchStatus, QualificationState
from app.services.orchestrator import PipelineOrchestrator, heuristic_analyze, orchestrator


# --------------------------------------------------------------------------- #
# FIXTURES
# --------------------------------------------------------------------------- #

@pytest.fixture
def web_session():
    """Thread-safe in-memory SQLite session with StaticPool."""
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
    """TestClient with transactional session override."""
    app.dependency_overrides[get_db] = lambda: web_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_csv_file(tmp_path: Path) -> Path:
    """Create a temporary CSV file with target contractors and non-target business."""
    csv_content = (
        "Name,Phone,Address,Facebook\n"
        "Kafue Earthmoving Ltd,+260977112233,Kafue,https://facebook.com/kafueearth\n"
        "Chilanga Quarry Logistics,+260977445566,Chilanga,https://facebook.com/chilangaql\n"
        "Lusaka Sweet Bakery,+260977998877,Lusaka,https://facebook.com/sweetbakery\n"
    )
    csv_file = tmp_path / "contractors.csv"
    csv_file.write_text(csv_content, encoding="utf-8")
    return csv_file


# --------------------------------------------------------------------------- #
# TESTS
# --------------------------------------------------------------------------- #

def test_orchestrator_chain_reaction(web_session: Session, sample_csv_file: Path):
    """
    Feeds sample raw data into orchestrator.run_pipeline().
    Asserts records move through all stages:
      DISCOVERED -> EVALUATING -> QUALIFIED -> STAGED_FOR_OUTREACH
    Confirms corresponding DispatchJob records are created in DRAFTED status.
    """
    orch = PipelineOrchestrator()

    # Run the complete autonomous chain reaction
    telemetry = orch.run_pipeline(
        files=[sample_csv_file],
        auto_qualify=True,
        auto_draft=True,
        db=web_session,
    )

    # 1. Ingestion check: 3 businesses created
    businesses = web_session.query(Business).all()
    assert len(businesses) == 3

    # 2. Qualification check:
    # Contractors should be QUALIFIED/STAGED_FOR_OUTREACH, Bakery should be DISQUALIFIED
    kafue_biz = web_session.query(Business).filter(Business.company_name.like("%Kafue%")).first()
    chilanga_biz = web_session.query(Business).filter(Business.company_name.like("%Chilanga%")).first()
    bakery_biz = web_session.query(Business).filter(Business.company_name.like("%Bakery%")).first()

    assert kafue_biz is not None
    assert chilanga_biz is not None
    assert bakery_biz is not None

    kafue_qual = kafue_biz.qualification
    chilanga_qual = chilanga_biz.qualification
    bakery_qual = bakery_biz.qualification

    # Bakery must be DISQUALIFIED
    assert bakery_qual.qualification_state == QualificationState.DISQUALIFIED.value

    # Qualified contractors must be transitioned to STAGED_FOR_OUTREACH
    assert kafue_qual.qualification_state == QualificationState.STAGED_FOR_OUTREACH.value
    assert chilanga_qual.qualification_state == QualificationState.STAGED_FOR_OUTREACH.value

    # 3. Outreach Job check: DispatchJob records created in DRAFTED
    jobs = web_session.query(DispatchJob).all()
    assert len(jobs) == 2  # Kafue and Chilanga

    for job in jobs:
        assert job.status == DispatchStatus.DRAFTED.value
        assert job.rendered_content is not None
        assert len(job.rendered_content.split()) <= 65
        assert "!" not in job.rendered_content

    # 4. Telemetry check: stage returns to IDLE after completion
    assert orch.current_stage == "IDLE"
    assert orch.is_running is False
    assert len(orch.logs) > 3


def test_telemetry_endpoint(client: TestClient, web_session: Session):
    """
    Polls GET /api/pipeline/telemetry and verifies state, progress percentage,
    kpis, and log history structure.
    """
    response = client.get("/api/pipeline/telemetry")
    assert response.status_code == 200

    data = response.json()
    assert "is_running" in data
    assert "current_stage" in data
    assert "progress_percent" in data
    assert "logs" in data
    assert isinstance(data["logs"], list)
    assert len(data["logs"]) > 0

    assert "kpis" in data
    kpis = data["kpis"]
    assert "discovered" in kpis
    assert "qualified" in kpis
    assert "dispatched" in kpis
    assert "revenue" in kpis


def test_master_run_all_endpoint(client: TestClient):
    """
    Tests POST /api/pipeline/run-all triggers full chain without errors.
    """
    response = client.post("/api/pipeline/run-all")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "SUCCESS"
    assert "Autonomous pipeline triggered" in data["message"]


def test_heuristic_analyzer_accuracy():
    """
    Confirms heuristic analyzer correctly separates industrial ICP vs non-ICP.
    """
    contractor = {
        "company_name": "Ndola Earthmoving & Plant Hire",
        "industry_sector": "Civil Construction",
        "description_raw": "Heavy excavation and road grading contractors in Copperbelt.",
        "location_district": "Ndola",
    }
    result_contractor = heuristic_analyze(contractor)
    assert result_contractor["sector_match"] is True
    assert result_contractor["primary_sector"] == "Civil Engineering"
    assert len(result_contractor["asset_footprint"]) > 0

    bakery = {
        "company_name": "Lusaka Artisan Bakery & Cafe",
        "industry_sector": "Food Retail",
        "description_raw": "Fresh bread and pastry shop.",
        "location_district": "Lusaka",
    }
    result_bakery = heuristic_analyze(bakery)
    assert result_bakery["sector_match"] is False
    assert result_bakery["confidence_score"] < 0.5
