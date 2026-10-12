"""
Layer 2 Ingestion Tests

Covers:
  1. CSV import with diverse header variations
  2. XLSX import
  3. Idempotent re-import (same file twice → 0 new businesses)
  4. Zambian phone normalization across all formats
  5. Entity deduplication: name variations + same phone → single business
  6. Dynamic header aliasing
  7. Source provenance storage
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

import pytest

from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import QualificationState
from app.services.ingestion.importer import import_file, _resolve_headers
from app.services.ingestion.normalizer import normalize_zambian_phone, extract_domain
from app.services.ingestion.deduplicator import (
    find_duplicate,
    merge_into_existing,
    _normalize_name,
)


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #

def _write_csv(filepath: Path, headers: list[str], rows: list[list[str]]):
    """Helper to write test CSV files."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)


# =========================================================================== #
#  Phone Normalization Tests
# =========================================================================== #

class TestZambianPhoneNormalization:
    """Test Zambian E.164 phone normalization."""

    def test_local_097_format(self):
        assert normalize_zambian_phone("0977112233") == "+260977112233"

    def test_local_096_format(self):
        assert normalize_zambian_phone("0966445566") == "+260966445566"

    def test_local_095_format(self):
        assert normalize_zambian_phone("0955998877") == "+260955998877"

    def test_local_077_format(self):
        assert normalize_zambian_phone("0771234567") == "+260771234567"

    def test_local_076_format(self):
        assert normalize_zambian_phone("0761234567") == "+260761234567"

    def test_local_075_format(self):
        assert normalize_zambian_phone("0751234567") == "+260751234567"

    def test_international_plus260(self):
        assert normalize_zambian_phone("+260966445566") == "+260966445566"

    def test_international_260_no_plus(self):
        assert normalize_zambian_phone("260955998877") == "+260955998877"

    def test_international_00260(self):
        assert normalize_zambian_phone("00260977112233") == "+260977112233"

    def test_with_spaces_and_dashes(self):
        assert normalize_zambian_phone("097 711 2233") == "+260977112233"
        assert normalize_zambian_phone("0977-112-233") == "+260977112233"

    def test_with_brackets(self):
        assert normalize_zambian_phone("(0977) 112233") == "+260977112233"

    def test_empty_string_returns_none(self):
        assert normalize_zambian_phone("") is None
        assert normalize_zambian_phone(None) is None
        assert normalize_zambian_phone("  ") is None

    def test_invalid_prefix_returns_none(self):
        assert normalize_zambian_phone("0991234567") is None

    def test_too_short_returns_none(self):
        assert normalize_zambian_phone("097711") is None

    def test_non_zambian_returns_none(self):
        assert normalize_zambian_phone("+1234567890") is None


# =========================================================================== #
#  URL / Domain Extraction Tests
# =========================================================================== #

class TestDomainExtraction:
    """Test URL normalization and domain extraction."""

    def test_full_url(self):
        assert extract_domain("https://www.copperbelt.co.zm/about") == "copperbelt.co.zm"

    def test_www_stripped(self):
        assert extract_domain("http://www.example.com") == "example.com"

    def test_bare_domain(self):
        assert extract_domain("example.co.zm") == "example.co.zm"

    def test_none_returns_none(self):
        assert extract_domain(None) is None
        assert extract_domain("") is None


# =========================================================================== #
#  Header Aliasing Tests
# =========================================================================== #

class TestHeaderAliasing:
    """Test dynamic header resolution from Instant Data Scraper exports."""

    def test_google_maps_headers(self):
        headers = ["title", "phone", "website", "address", "category"]
        mapping = _resolve_headers(headers)
        assert mapping["company_name"] == 0  # "title"
        assert mapping["phone"] == 1
        assert mapping["website"] == 2
        assert mapping["location"] == 3   # "address"
        assert mapping["industry"] == 4   # "category"

    def test_facebook_headers(self):
        headers = ["name", "mobile", "profile_url", "city", "type"]
        mapping = _resolve_headers(headers)
        assert mapping["company_name"] == 0
        assert mapping["phone"] == 1
        assert mapping["fb_page_url"] == 2
        assert mapping["location"] == 3
        assert mapping["industry"] == 4

    def test_web_directory_headers(self):
        headers = ["business_name", "telephone", "link", "district", "sector"]
        mapping = _resolve_headers(headers)
        assert mapping["company_name"] == 0
        assert mapping["phone"] == 1
        assert mapping["website"] == 2
        assert mapping["location"] == 3
        assert mapping["industry"] == 4

    def test_missing_columns_return_none(self):
        headers = ["name", "some_other_col"]
        mapping = _resolve_headers(headers)
        assert mapping["company_name"] == 0
        assert mapping["phone"] is None
        assert mapping["website"] is None


# =========================================================================== #
#  Name Normalization Tests
# =========================================================================== #

class TestNameNormalization:
    """Test corporate suffix stripping and normalization."""

    def test_strip_ltd(self):
        assert _normalize_name("Kafue Earthmoving Ltd") == "kafue earthmoving"

    def test_strip_limited(self):
        assert _normalize_name("Apex Mining Limited") == "apex mining"

    def test_strip_plc(self):
        assert _normalize_name("Apex Mining Infrastructure Plc") == "apex mining infrastructure"

    def test_case_insensitive(self):
        assert _normalize_name("KAFUE EARTHMOVING LTD") == "kafue earthmoving"


# =========================================================================== #
#  CSV Import Tests
# =========================================================================== #

class TestCSVImport:
    """Test CSV file import with aliased headers."""

    def test_basic_csv_import(self, session):
        """Import a simple CSV and verify businesses are created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "test.csv"
            _write_csv(csv_path, ["name", "phone", "category", "location"], [
                ["Test Company A", "0977112233", "Civil Engineering", "Lusaka"],
                ["Test Company B", "0966445566", "Mining", "Kitwe"],
            ])

            summary = import_file(session, csv_path)

        assert summary.rows_read == 2
        assert summary.valid_records == 2
        assert summary.new_businesses == 2
        assert summary.duplicates_skipped == 0

        # Verify businesses in DB
        businesses = session.query(Business).all()
        assert len(businesses) == 2

        # Verify qualifications created
        quals = session.query(ProspectQualification).all()
        assert len(quals) == 2
        assert all(q.qualification_state == QualificationState.DISCOVERED.value for q in quals)

    def test_phone_normalization_on_import(self, session):
        """Verify phone numbers are normalized during import."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "phones.csv"
            _write_csv(csv_path, ["name", "phone"], [
                ["Company A", "0977112233"],
                ["Company B", "+260966445566"],
            ])

            import_file(session, csv_path)

        businesses = session.query(Business).order_by(Business.company_name).all()
        assert businesses[0].phone_normalized == "+260977112233"
        assert businesses[1].phone_normalized == "+260966445566"

    def test_missing_fields_stored_as_none(self, session):
        """Fields not present in CSV are stored as None, never fabricated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "minimal.csv"
            _write_csv(csv_path, ["name"], [
                ["Minimal Corp"],
            ])

            import_file(session, csv_path)

        biz = session.query(Business).first()
        assert biz.company_name == "Minimal Corp"
        assert biz.phone_raw is None
        assert biz.phone_normalized is None
        assert biz.email is None
        assert biz.website_url is None

    def test_idempotent_reimport(self, session):
        """Importing the exact same CSV twice produces 0 new businesses."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "idempotent.csv"
            _write_csv(csv_path, ["name", "phone", "category"], [
                ["Kafue Earth Ltd", "0977112233", "Earthworks"],
                ["Kitwe Mining Co", "0966445566", "Mining"],
            ])

            s1 = import_file(session, csv_path)
            assert s1.new_businesses == 2

            s2 = import_file(session, csv_path)
            assert s2.new_businesses == 0
            assert s2.duplicates_skipped + s2.duplicates_merged == 2

        # Total businesses still 2
        total = session.query(Business).count()
        assert total == 2

    def test_source_provenance_stored(self, session):
        """Verify source_provenance JSON includes filename and metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "provenance_test.csv"
            _write_csv(csv_path, ["name", "phone", "extra_col"], [
                ["Test Co", "0977112233", "extra_data"],
            ])

            import_file(session, csv_path)

        biz = session.query(Business).first()
        prov = biz.source_provenance
        assert prov is not None
        assert prov["source_file"] == "provenance_test.csv"
        assert "extraction_date" in prov
        assert "unmapped_columns" in prov
        assert "extra_col" in prov["unmapped_columns"]

    def test_empty_rows_skipped(self, session):
        """Rows with no company name are quarantined."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "empty.csv"
            _write_csv(csv_path, ["name", "phone"], [
                ["Valid Company", "0977112233"],
                ["", "0966445566"],
                ["  ", "0955998877"],
            ])

            summary = import_file(session, csv_path)

        assert summary.valid_records == 1
        assert summary.invalid_quarantined == 2
        assert summary.new_businesses == 1


# =========================================================================== #
#  XLSX Import Tests
# =========================================================================== #

class TestXLSXImport:
    """Test XLSX file import."""

    def test_basic_xlsx_import(self, session):
        """Import a simple XLSX and verify businesses are created."""
        import openpyxl

        with tempfile.TemporaryDirectory() as tmpdir:
            xlsx_path = Path(tmpdir) / "test.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["company_name", "telephone", "sector", "district"])
            ws.append(["XLSX Company Alpha", "0977111111", "Plant Hire", "Lusaka"])
            ws.append(["XLSX Company Beta", "0966222222", "Mining", "Ndola"])
            wb.save(xlsx_path)

            summary = import_file(session, xlsx_path)

        assert summary.rows_read == 2
        assert summary.new_businesses == 2

        biz = session.query(Business).filter(
            Business.company_name == "XLSX Company Alpha"
        ).first()
        assert biz is not None
        assert biz.phone_normalized == "+260977111111"


# =========================================================================== #
#  Deduplication Tests
# =========================================================================== #

class TestDeduplication:
    """Test entity deduplication across import runs."""

    def test_phone_based_dedup(self, session):
        """Two rows with the same phone resolve to a single business."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "dedup_phone.csv"
            _write_csv(csv_path, ["name", "phone"], [
                ["Kafue Earthmoving", "0977112233"],
                ["Kafue Earth Moving Ltd", "0977112233"],
            ])

            summary = import_file(session, csv_path)

        assert summary.new_businesses == 1
        assert summary.duplicates_skipped + summary.duplicates_merged >= 1
        assert session.query(Business).count() == 1

    def test_name_variation_same_district_dedup(self, session):
        """Slight name variations in the same district are deduplicated."""
        # First import
        biz = Business(
            company_name="Kafue Earthmoving Ltd",
            location_district="Kafue",
            source_type="MANUAL",
        )
        session.add(biz)
        session.flush()

        # Now check deduplication
        match = find_duplicate(
            session,
            phone_normalized=None,
            website_url=None,
            company_name="Kafue Earthmoving Limited",
            location_district="Kafue",
        )
        assert match is not None
        assert match.id == biz.id

    def test_merge_fills_empty_fields(self, session):
        """Merge fills in None fields without overwriting existing data."""
        biz = Business(
            company_name="Test Co",
            phone_raw="0977112233",
            phone_normalized="+260977112233",
            email=None,
            website_url=None,
            source_type="MANUAL",
        )
        session.add(biz)
        session.flush()

        updated = merge_into_existing(biz, {
            "email": "test@example.com",
            "website_url": "https://test.co.zm",
            "phone_raw": "different_phone",  # should NOT overwrite
        })

        assert updated is True
        assert biz.email == "test@example.com"
        assert biz.website_url == "https://test.co.zm"
        assert biz.phone_raw == "0977112233"  # NOT overwritten


# =========================================================================== #
#  Sample Dataset Import Test
# =========================================================================== #

class TestSampleDatasetImport:
    """Test importing the actual sample dataset."""

    def test_sample_csv_imports_all_five(self, session):
        """The sample CSV produces 5 new businesses."""
        csv_path = Path(__file__).parent.parent / "data" / "input" / "sample_zambian_contractors.csv"
        if not csv_path.exists():
            pytest.skip("Sample CSV not found")

        summary = import_file(session, csv_path)

        assert summary.rows_read == 5
        assert summary.new_businesses == 5
        assert summary.invalid_quarantined == 0

        # All should have DISCOVERED qualifications
        quals = session.query(ProspectQualification).all()
        assert len(quals) == 5
        assert all(q.qualification_state == QualificationState.DISCOVERED.value for q in quals)


# =========================================================================== #
#  Adaptive Facebook Scraper Ingestion Tests
# =========================================================================== #

class TestAdaptiveFacebookScraperIngestion:
    """Tests for Instant Data Scraper exports containing minified Facebook headers."""

    def test_facebook_instant_data_scraper_ingestion(self, session, tmp_path):
        """
        Ingest a sample CSV matching exact minified Facebook headers (x1lliihq, x1i10hfl, html-span, etc.).
        Verify extraction of:
          - Real businesses:
              'Notions Electrical Contractors Zambia'
              'Hypersonic Contractors Limited'
              'ICU Construction'
              'Tribliza Construction Company'
              'PG GLASS Zambia'
          - Phone numbers from post text (0974176967 -> +260974176967)
          - Clean Facebook Page URLs without tracking noise
        """
        headers = [
            "x1lliihq",
            "x1i10hfl href",
            "x1i10hfl",
            "x1i10hfl href 2",
            "html-span",
            "html-span 2",
            "xdj266r",
            "xdj266r 2",
            "html-span 3",
            "html-span 4",
        ]

        rows = [
            # 1. Notions Electrical Contractors Zambia
            [
                "Pages",
                "https://web.facebook.com/NotionsElectricalContractors?__tn__=%3C",
                "Notions Electrical Contractors Zambia",
                "https://web.facebook.com/NotionsElectricalContractors",
                "Closed now",
                "Closed now",
                "",
                "",
                "Electrical contracting and solar installations in Lusaka",
                "Call us today for commercial and industrial electrical work.",
            ],
            # 2. Contractor with embedded phone in post text
            [
                "by0IW.com",
                "https://web.facebook.com/joh0974?__cft__[0]=AZgfm5KK44&__tn__=%3C%3C%2CP-R",
                "See more",
                "https://web.facebook.com/joh0974?__cft__[0]=AZgfm5KK44",
                "dm contractors zambia",
                "·",
                "Dm contractor Zambia",
                "Only k900,0000 4 bedroom house…",
                "Foundry",
                "Dm contractor Zambia\nOnly k900,000 4 bedroom house\nLabour and material finish 0974176967",
            ],
            # 3. Hypersonic Contractors Limited
            [
                "5Qb45Z.com",
                "https://web.facebook.com/profile.php?id=100063567901947&__cft__[0]=AZhEuJwD&__tn__=%3C%3C%2CP-R",
                "See more",
                "https://web.facebook.com/profile.php?id=100063567901947",
                "Hypersonic Contractors Limited",
                "·",
                "Building strong. Building right. Building to last.",
                "",
                "Photos from Hypersonic Contractors Limited's post",
                "Civil works, building construction, and earth moving.",
            ],
            # 4. ICU Construction
            [
                "A7JZFukN0.com",
                "https://web.facebook.com/profile.php?id=61592088656227&__cft__[0]=AZguHHxH&__tn__=%3C%3C%2CP-R",
                "See more",
                "https://web.facebook.com/profile.php?id=61592088656227",
                "ICU Construction",
                "·",
                "WHAT ARE YOU BUILDING THIS WEEK?",
                "",
                "Photos from ICU Construction 's post",
                "High quality residential and commercial construction services.",
            ],
            # 5. Tribliza Construction Company
            [
                "ZxQcdpCcw.com",
                "https://web.facebook.com/profile.php?id=61556753156193&__cft__[0]=AZh3pa74&__tn__=%3C%3C%2CP-R",
                "See more",
                "https://web.facebook.com/profile.php?id=61556753156193",
                "Tribliza Construction Company",
                "·",
                "Building Houses, Smart Work Done",
                "",
                "Photos from Tribliza Construction Company's post",
                "General construction, architectural planning, and bricklaying.",
            ],
            # 6. PG GLASS Zambia
            [
                "K3yy5I0gs.com",
                "https://web.facebook.com/profile.php?id=100064061885891&__cft__[0]=AZiwBPi3&__tn__=%3C%3C%2CP-R",
                "See more",
                "https://web.facebook.com/profile.php?id=100064061885891",
                "PG GLASS Zambia",
                "·",
                "This truck is carrying something that can transform an entire building...",
                "",
                "Photos from PG GLASS Zambia's post",
                "Architectural glass, aluminum frames, and building glazing.",
            ],
        ]

        csv_file = tmp_path / "facebook_scraper_test.csv"
        _write_csv(csv_file, headers, rows)

        summary = import_file(session, csv_file)

        assert summary.rows_read == 6
        assert summary.new_businesses >= 5
        assert summary.errors == []

        # Verify extracted businesses exist
        biz_notions = session.query(Business).filter(Business.company_name.like("%Notions Electrical%")).first()
        assert biz_notions is not None
        assert biz_notions.fb_page_url == "https://web.facebook.com/NotionsElectricalContractors"

        biz_hypersonic = session.query(Business).filter(Business.company_name.like("%Hypersonic Contractors%")).first()
        assert biz_hypersonic is not None
        assert biz_hypersonic.fb_page_url == "https://web.facebook.com/profile.php?id=100063567901947"

        biz_icu = session.query(Business).filter(Business.company_name.like("%ICU Construction%")).first()
        assert biz_icu is not None
        assert biz_icu.fb_page_url == "https://web.facebook.com/profile.php?id=61592088656227"

        biz_tribliza = session.query(Business).filter(Business.company_name.like("%Tribliza Construction%")).first()
        assert biz_tribliza is not None
        assert biz_tribliza.fb_page_url == "https://web.facebook.com/profile.php?id=61556753156193"

        biz_pg = session.query(Business).filter(Business.company_name.like("%PG GLASS%")).first()
        assert biz_pg is not None
        assert biz_pg.fb_page_url == "https://web.facebook.com/profile.php?id=100064061885891"

        # Verify deep phone extraction from post text
        biz_phone = session.query(Business).filter(Business.phone_normalized == "+260974176967").first()
        assert biz_phone is not None
        assert "contractor" in biz_phone.company_name.lower()

    def test_actual_web_3_csv_import(self, session):
        """Verify that actual web (3).csv imports cleanly."""
        csv_path = Path(__file__).parent.parent / "data" / "input" / "web (3).csv"
        if not csv_path.exists():
            pytest.skip("web (3).csv not found")

        summary = import_file(session, csv_path)
        assert summary.rows_read > 0
        assert summary.new_businesses > 0
        assert summary.errors == []
