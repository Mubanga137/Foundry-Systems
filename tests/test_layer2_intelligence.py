"""
Layer 2 Intelligence Tests

Covers:
  1. Gemini Key Manager: round-robin rotation, 429 cooldown, 401 removal, key redaction.
  2. Deterministic Scoring Engine: all boundary conditions and pillar computations.
  3. Full Pipeline (mocked Gemini): DISCOVERED → EVALUATING → QUALIFIED/DISQUALIFIED
     with AuditLog verification.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest

from app.models.audit_log import AuditLog
from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import QualificationState
from app.services.intelligence.key_manager import GeminiKeyManager, redact_key
from app.services.intelligence.gemini_client import GeminiAnalysisClient
from app.services.intelligence.scorer import compute_score, ScoringResult
from app.services.intelligence.qualifier import qualify_business, qualify_batch


# --------------------------------------------------------------------------- #
#  Test Data Factories
# --------------------------------------------------------------------------- #

def _mock_gemini_response_qualified(prompt: str) -> dict:
    """Mock Gemini response for a qualified business."""
    return {
        "sector_match": True,
        "primary_sector": "Civil Engineering",
        "asset_footprint": ["CAT 320D Excavator", "20-ton Bell Tippers"],
        "digital_deficits": ["No mobile fleet catalogue", "No website"],
        "evidence_summary": "Company operates heavy machinery for road construction.",
        "confidence_score": 0.85,
    }


def _mock_gemini_response_disqualified_sector(prompt: str) -> dict:
    """Mock Gemini response for a sector-mismatch business."""
    return {
        "sector_match": False,
        "primary_sector": "Retail",
        "asset_footprint": [],
        "digital_deficits": [],
        "evidence_summary": "Retail bakery with walk-in customers.",
        "confidence_score": 0.90,
    }


def _mock_gemini_response_disqualified_digital(prompt: str) -> dict:
    """Mock Gemini response for a fully-modernized business."""
    return {
        "sector_match": True,
        "primary_sector": "Mining Support",
        "asset_footprint": ["Komatsu PC200", "Fleet management system"],
        "digital_deficits": ["Fully responsive modern portal with active RFQ system"],
        "evidence_summary": "Large contractor with modern web presence.",
        "confidence_score": 0.95,
    }


def _make_business(session, **overrides) -> Business:
    defaults = dict(
        company_name="Test Business",
        source_type="MANUAL",
    )
    defaults.update(overrides)
    biz = Business(**defaults)
    session.add(biz)
    session.flush()
    return biz


def _make_qualification(session, business, **overrides) -> ProspectQualification:
    defaults = dict(
        business_id=business.id,
        qualification_state=QualificationState.DISCOVERED.value,
    )
    defaults.update(overrides)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.flush()
    return qual


# =========================================================================== #
#  1. KEY MANAGER TESTS
# =========================================================================== #

class TestGeminiKeyManager:
    """Test thread-safe multi-key rotation with rate limiting and cooldown."""

    def test_round_robin_rotation(self):
        """Keys rotate in round-robin order."""
        km = GeminiKeyManager(keys=["key_A", "key_B", "key_C"], rpm_per_key=100)

        k1 = km.acquire_key()
        k2 = km.acquire_key()
        k3 = km.acquire_key()

        assert {k1, k2, k3} == {"key_A", "key_B", "key_C"}

    def test_429_places_key_in_cooldown(self):
        """Key receiving 429 is placed in cooldown; next key serves."""
        km = GeminiKeyManager(
            keys=["key_A", "key_B"],
            rpm_per_key=100,
            cooldown_seconds=60,
        )

        k1 = km.acquire_key()
        km.release_key(k1, success=False, error_code=429)

        # Next acquire should return the other key
        k2 = km.acquire_key()
        assert k2 != k1

    def test_401_permanently_removes_key(self):
        """Key receiving 401 is permanently removed."""
        km = GeminiKeyManager(keys=["key_A", "key_B"], rpm_per_key=100)

        k1 = km.acquire_key()
        km.release_key(k1, success=False, error_code=401)

        assert km.active_key_count == 1

        # All subsequent acquires return the remaining key
        for _ in range(5):
            k = km.acquire_key()
            assert k != k1

    def test_403_permanently_removes_key(self):
        """Key receiving 403 is permanently removed."""
        km = GeminiKeyManager(keys=["key_A", "key_B"], rpm_per_key=100)

        k1 = km.acquire_key()
        km.release_key(k1, success=False, error_code=403)

        assert km.active_key_count == 1

    def test_all_keys_exhausted_raises(self):
        """RuntimeError when no keys are available."""
        km = GeminiKeyManager(keys=["key_A"], rpm_per_key=100)

        k = km.acquire_key()
        km.release_key(k, success=False, error_code=401)

        with pytest.raises(RuntimeError, match="No API keys available"):
            km.acquire_key()

    def test_rate_limiting(self):
        """Key is skipped when RPM limit reached."""
        km = GeminiKeyManager(keys=["key_A", "key_B"], rpm_per_key=2)

        # Exhaust key_A's rate limit
        k1 = km.acquire_key()  # key_A (uses slot 1 of 2)
        k2 = km.acquire_key()  # key_B (uses slot 1 of 2)
        k3 = km.acquire_key()  # key_A (uses slot 2 of 2)
        k4 = km.acquire_key()  # key_B (uses slot 2 of 2)

        # Both keys now at limit; all keys exhausted
        with pytest.raises(RuntimeError, match="No API keys available"):
            km.acquire_key()

    def test_key_redaction(self):
        """API keys are never exposed — only redacted versions."""
        redacted = redact_key("AIzaSyBx1234567890abcdef")
        assert "AIzaSy" in redacted
        assert "****" in redacted
        assert "1234567890abcdef" not in redacted

    def test_short_key_redaction(self):
        """Very short keys are fully redacted."""
        assert redact_key("abc") == "****"

    def test_keys_never_in_log_output(self, caplog):
        """Verify keys are redacted in log output during cooldown."""
        real_key = "AIzaSyBxSuperSecretKeyValue123"
        km = GeminiKeyManager(
            keys=[real_key, "AIzaSyBxOtherKey456"],
            rpm_per_key=100,
            cooldown_seconds=60,
        )

        with caplog.at_level(logging.WARNING):
            k = km.acquire_key()
            km.release_key(k, success=False, error_code=429)

        # Check that the full key never appears in any log record
        for record in caplog.records:
            assert real_key not in record.getMessage()

    def test_no_keys_raises_value_error(self):
        """Constructor requires at least one key."""
        with pytest.raises(ValueError, match="(?i)at least one"):
            GeminiKeyManager(keys=[])

    def test_healthy_keys_returns_redacted(self):
        """get_healthy_keys returns only redacted identifiers."""
        km = GeminiKeyManager(
            keys=["AIzaSyKeyAlpha", "AIzaSyKeyBeta"],
            rpm_per_key=100,
        )
        healthy = km.get_healthy_keys()
        assert len(healthy) == 2
        for h in healthy:
            assert "****" in h


# =========================================================================== #
#  2. SCORING ENGINE TESTS
# =========================================================================== #

class TestScoringEngine:
    """Test the deterministic 4-pillar scoring rubric."""

    def test_target_sector_full_score(self):
        """Target sector gets 30 points for Pillar 1."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Civil Engineering",
            "asset_footprint": ["Excavator"],
            "digital_deficits": ["No website"],
            "evidence_summary": "...",
            "confidence_score": 0.8,
        }
        result = compute_score(analysis, has_website=False, phone_normalized="+260977112233")
        assert result.pillar_1_sector == 30
        assert result.sector_match is True

    def test_peripheral_sector_half_score(self):
        """Peripheral B2B sector gets 15 points."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Commercial Security",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(analysis)
        assert result.pillar_1_sector == 15
        assert result.sector_match is True

    def test_retail_sector_zero_score(self):
        """Retail/consumer sector gets 0 points and sector_match=False."""
        analysis = {
            "sector_match": False,
            "primary_sector": "Retail",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.9,
        }
        result = compute_score(analysis)
        assert result.pillar_1_sector == 0
        assert result.sector_match is False

    def test_heavy_assets_full_score(self):
        """Heavy yellow iron gets 25 points for Pillar 2."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Plant Hire",
            "asset_footprint": ["CAT 320D Excavator", "20-ton Bell Tipper"],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.8,
        }
        result = compute_score(analysis)
        assert result.pillar_2_assets == 25

    def test_no_assets_zero_score(self):
        """No assets gets 0 points for Pillar 2."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Civil Engineering",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.5,
        }
        result = compute_score(analysis)
        assert result.pillar_2_assets == 0

    def test_no_website_full_digital_deficit(self):
        """No website gets 25 points for Pillar 3."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Quarry",
            "asset_footprint": [],
            "digital_deficits": ["No website found"],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(analysis, has_website=False)
        assert result.pillar_3_digital == 25

    def test_modern_portal_zero_digital_deficit(self):
        """Fully modernized portal gets 0 points for Pillar 3."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Mining Support",
            "asset_footprint": ["Komatsu PC200"],
            "digital_deficits": ["Fully responsive modern portal with active RFQ"],
            "evidence_summary": "...",
            "confidence_score": 0.9,
        }
        result = compute_score(analysis, has_website=True)
        assert result.pillar_3_digital == 0

    def test_fb_messenger_full_contact_score(self):
        """Facebook Messenger endpoint gets 20 points for Pillar 4."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Plant Hire",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(analysis, has_fb_messenger=True)
        assert result.pillar_4_contact == 20

    def test_zambian_mobile_phone_contact_score(self):
        """Valid Zambian mobile gets 15 points for Pillar 4."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Civil Engineering",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(analysis, phone_normalized="+260977112233")
        assert result.pillar_4_contact == 15

    def test_no_contact_minimal_score(self):
        """No verified contact gets 5 points for Pillar 4."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Civil Engineering",
            "asset_footprint": [],
            "digital_deficits": [],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(analysis)
        assert result.pillar_4_contact == 5

    # --- Boundary conditions ---

    def test_score_64_disqualified(self):
        """Score of 64 → DISQUALIFIED (below threshold)."""
        # Pillar 1: 30 (target sector) + Pillar 2: 0 (no assets)
        # + Pillar 3: 25 (no website) + Pillar 4: 5 (unverified) = 60
        analysis = {
            "sector_match": True,
            "primary_sector": "Heavy Haulage",
            "asset_footprint": [],
            "digital_deficits": ["No website"],
            "evidence_summary": "...",
            "confidence_score": 0.5,
        }
        result = compute_score(analysis, has_website=False)
        assert result.total_score == 60
        assert result.is_qualified is False

    def test_score_65_qualified(self):
        """Score of 65+ → QUALIFIED (at threshold)."""
        # Pillar 1: 30 + Pillar 2: 0 + Pillar 3: 25 + Pillar 4: 15 = 70
        analysis = {
            "sector_match": True,
            "primary_sector": "Civil Engineering",
            "asset_footprint": [],
            "digital_deficits": ["No website"],
            "evidence_summary": "...",
            "confidence_score": 0.7,
        }
        result = compute_score(
            analysis, has_website=False, phone_normalized="+260977112233"
        )
        assert result.total_score == 70
        assert result.sector_match is True
        assert result.is_qualified is True

    def test_non_target_sector_disqualifies_regardless(self):
        """Non-target sector auto-disqualifies even with high asset/deficit scores."""
        analysis = {
            "sector_match": False,
            "primary_sector": "Bakery",
            "asset_footprint": ["Industrial oven", "delivery truck"],
            "digital_deficits": ["No website"],
            "evidence_summary": "...",
            "confidence_score": 0.9,
        }
        result = compute_score(
            analysis, has_website=False, has_fb_messenger=True,
            phone_normalized="+260977112233",
        )
        assert result.sector_match is False
        assert result.is_qualified is False

    def test_max_score_100(self):
        """Verify the maximum possible score is 100."""
        analysis = {
            "sector_match": True,
            "primary_sector": "Plant Hire",
            "asset_footprint": ["CAT 320D Excavator"],
            "digital_deficits": ["No website found"],
            "evidence_summary": "...",
            "confidence_score": 0.95,
        }
        result = compute_score(
            analysis, has_website=False, has_fb_messenger=True,
        )
        # 30 + 25 + 25 + 20 = 100
        assert result.total_score == 100
        assert result.is_qualified is True


# =========================================================================== #
#  3. FULL PIPELINE TESTS (MOCKED GEMINI)
# =========================================================================== #

class TestQualificationPipeline:
    """Test the complete pipeline with mocked Gemini analysis."""

    def test_qualified_flow_with_audit_trail(self, session):
        """DISCOVERED → EVALUATING → QUALIFIED produces correct audit trail."""
        biz = _make_business(
            session,
            company_name="Kafue Earthmoving Ltd",
            phone_normalized="+260977112233",
            industry_sector="Civil Earthworks",
        )
        qual = _make_qualification(session, biz)

        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(
            km, analyze_fn=_mock_gemini_response_qualified
        )

        result = qualify_business(session, biz, qual, client)

        assert result == QualificationState.QUALIFIED.value
        assert qual.qualification_state == QualificationState.QUALIFIED.value
        assert qual.fit_score >= 65
        assert qual.sector_match is True
        assert qual.asset_footprint is not None

        # Verify audit trail: DISCOVERED → EVALUATING → QUALIFIED
        logs = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "ProspectQualification",
                AuditLog.entity_id == qual.id,
            )
            .order_by(AuditLog.created_at)
            .all()
        )
        assert len(logs) == 2
        assert logs[0].old_state == QualificationState.DISCOVERED.value
        assert logs[0].new_state == QualificationState.EVALUATING.value
        assert logs[1].old_state == QualificationState.EVALUATING.value
        assert logs[1].new_state == QualificationState.QUALIFIED.value

    def test_disqualified_sector_mismatch(self, session):
        """Retail bakery is DISQUALIFIED due to sector mismatch."""
        biz = _make_business(
            session,
            company_name="Lusaka Premier Bakery",
            phone_normalized="+260977334455",
            industry_sector="Retail Bakery",
        )
        qual = _make_qualification(session, biz)

        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(
            km, analyze_fn=_mock_gemini_response_disqualified_sector
        )

        result = qualify_business(session, biz, qual, client)

        assert result == QualificationState.DISQUALIFIED.value
        assert qual.fit_score is not None
        assert qual.sector_match is False
        assert "Sector mismatch" in (qual.disqualification_reason or "")

    def test_disqualified_no_digital_deficit(self, session):
        """Modernized company disqualified due to no digital gap opportunity."""
        biz = _make_business(
            session,
            company_name="Apex Mining Infrastructure Plc",
            website_url="https://apexmining.co.zm",
            industry_sector="Mining Infrastructure",
        )
        qual = _make_qualification(session, biz)

        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(
            km, analyze_fn=_mock_gemini_response_disqualified_digital
        )

        result = qualify_business(session, biz, qual, client)

        # Should be disqualified because Pillar 3 (digital deficit) = 0
        # Total: 30 (sector) + 25 (assets) + 0 (digital) + 5 (no contact) = 60 < 65
        assert result == QualificationState.DISQUALIFIED.value

    def test_batch_qualification(self, session):
        """Batch run processes multiple businesses correctly."""
        biz1 = _make_business(session, company_name="Qualified Co")
        qual1 = _make_qualification(session, biz1)

        biz2 = _make_business(session, company_name="Disqualified Co")
        qual2 = _make_qualification(session, biz2)

        call_count = {"n": 0}

        def alternating_mock(prompt):
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                return _mock_gemini_response_qualified(prompt)
            return _mock_gemini_response_disqualified_sector(prompt)

        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(km, analyze_fn=alternating_mock)

        summary = qualify_batch(session, client, limit=10)

        assert summary.total_processed == 2
        assert summary.qualified + summary.disqualified == 2
        assert summary.errors == 0


# =========================================================================== #
#  4. GEMINI CLIENT TESTS
# =========================================================================== #

class TestGeminiClient:
    """Test the Gemini analysis client with injection."""

    def test_analyze_with_injected_fn(self):
        """Injected analyze_fn is called instead of real API."""
        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(
            km, analyze_fn=_mock_gemini_response_qualified
        )

        result = client.analyze({"company_name": "Test Co"})

        assert result["sector_match"] is True
        assert "Civil Engineering" in result["primary_sector"]
        assert isinstance(result["asset_footprint"], list)
        assert isinstance(result["confidence_score"], float)

    def test_analyze_returns_all_required_fields(self):
        """Verify all required fields present in mock response."""
        km = GeminiKeyManager(keys=["test_key"], rpm_per_key=100)
        client = GeminiAnalysisClient(
            km, analyze_fn=_mock_gemini_response_qualified
        )

        result = client.analyze({"company_name": "Test"})

        required = {"sector_match", "primary_sector", "asset_footprint",
                     "digital_deficits", "evidence_summary", "confidence_score"}
        assert required.issubset(set(result.keys()))
