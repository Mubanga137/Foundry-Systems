"""
Flexible CSV/XLSX Importer for Instant Data Scraper exports.

Dynamic header aliasing accommodates inconsistent column names
from Google Maps, Facebook search, and web directory scrapes.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import QualificationState
from app.services.ingestion.normalizer import normalize_zambian_phone
from app.services.ingestion.deduplicator import find_duplicate, merge_into_existing

# --------------------------------------------------------------------------- #
#  Header alias maps
# --------------------------------------------------------------------------- #

HEADER_ALIASES: dict[str, list[str]] = {
    "company_name": ["name", "company", "company_name", "title", "business_name"],
    "phone": ["phone", "telephone", "mobile", "contact", "phone_number"],
    "website": ["website", "url", "web", "site", "link"],
    "fb_page_url": ["facebook", "fb_url", "facebook_page", "profile_url"],
    "location": ["location", "address", "city", "district", "area"],
    "industry": ["category", "industry", "type", "sector", "description"],
    "email": ["email", "email_address", "e-mail", "mail"],
}


@dataclass
class ImportSummary:
    """Summary of a single import run."""
    filename: str = ""
    rows_read: int = 0
    valid_records: int = 0
    new_businesses: int = 0
    duplicates_skipped: int = 0
    duplicates_merged: int = 0
    invalid_quarantined: int = 0
    errors: list[str] = field(default_factory=list)


def _resolve_headers(raw_headers: list[str]) -> dict[str, Optional[int]]:
    """
    Map standard field names to column indices using alias lookup.
    Returns {standard_name: column_index_or_None}.
    """
    mapping: dict[str, Optional[int]] = {key: None for key in HEADER_ALIASES}
    normalized = [h.strip().lower().replace(" ", "_") for h in raw_headers]

    for std_name, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[std_name] = normalized.index(alias)
                break

    return mapping


def _extract_row(row: list[str], col_map: dict[str, Optional[int]]) -> dict[str, Any]:
    """Extract values from a row using the resolved column map."""
    data = {}
    for field_name, idx in col_map.items():
        if idx is not None and idx < len(row):
            val = row[idx].strip() if row[idx] else None
            data[field_name] = val if val else None
        else:
            data[field_name] = None
    return data


def _read_csv(filepath: Path) -> tuple[list[str], list[list[str]]]:
    """Read a CSV file, returning (headers, rows)."""
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    return headers, rows


def _read_xlsx(filepath: Path) -> tuple[list[str], list[list[str]]]:
    """Read an XLSX file, returning (headers, rows)."""
    import openpyxl
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active
    all_rows = []
    for row in ws.iter_rows(values_only=True):
        all_rows.append([str(cell) if cell is not None else "" for cell in row])
    wb.close()
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def import_file(
    session: Session,
    filepath: str | Path,
    source_type: str = "INSTANT_DATA_SCRAPER",
) -> ImportSummary:
    """
    Import businesses from a CSV or XLSX file.

    - Resolves dynamic headers via alias matching.
    - Normalizes Zambian phone numbers to E.164.
    - Deduplicates against existing Business records.
    - Creates ProspectQualification in DISCOVERED state for new records.
    - Stores source provenance metadata.
    """
    filepath = Path(filepath)
    summary = ImportSummary(filename=filepath.name)

    if not filepath.exists():
        summary.errors.append(f"File not found: {filepath}")
        return summary

    ext = filepath.suffix.lower()
    if ext == ".csv":
        headers, rows = _read_csv(filepath)
    elif ext in (".xlsx", ".xls"):
        headers, rows = _read_xlsx(filepath)
    else:
        summary.errors.append(f"Unsupported file type: {ext}")
        return summary

    col_map = _resolve_headers(headers)

    if col_map["company_name"] is None:
        summary.errors.append(
            f"No company name column found. Headers: {headers}"
        )
        return summary

    summary.rows_read = len(rows)

    # Collect unmapped column indices for provenance
    mapped_indices = {idx for idx in col_map.values() if idx is not None}
    unmapped_headers = [
        headers[i] for i in range(len(headers)) if i not in mapped_indices
    ]

    for row_idx, row in enumerate(rows):
        try:
            data = _extract_row(row, col_map)

            company_name = data.get("company_name")
            if not company_name:
                summary.invalid_quarantined += 1
                continue

            summary.valid_records += 1

            # Normalize phone
            phone_raw = data.get("phone")
            phone_normalized = normalize_zambian_phone(phone_raw)

            # Check for duplicate
            existing = find_duplicate(
                session,
                phone_normalized=phone_normalized,
                website_url=data.get("website"),
                company_name=company_name,
                location_district=data.get("location"),
            )

            if existing:
                merge_data = {
                    "phone_raw": phone_raw,
                    "phone_normalized": phone_normalized,
                    "email": data.get("email"),
                    "website_url": data.get("website"),
                    "fb_page_url": data.get("fb_page_url"),
                    "industry_sector": data.get("industry"),
                    "location_district": data.get("location"),
                    "description_raw": data.get("industry"),  # industry often contains description
                }
                merged = merge_into_existing(existing, merge_data)
                if merged:
                    summary.duplicates_merged += 1
                else:
                    summary.duplicates_skipped += 1
                continue

            # Build source provenance
            provenance = {
                "source_file": filepath.name,
                "extraction_date": datetime.now(timezone.utc).isoformat(),
                "row_index": row_idx,
                "unmapped_columns": {
                    headers[i]: (row[i].strip() if i < len(row) and row[i] else None)
                    for i in range(len(headers))
                    if i not in mapped_indices
                },
            }

            # Create new Business
            business = Business(
                company_name=company_name,
                phone_raw=phone_raw,
                phone_normalized=phone_normalized,
                email=data.get("email"),
                website_url=data.get("website"),
                fb_page_url=data.get("fb_page_url"),
                industry_sector=data.get("industry"),
                location_district=data.get("location"),
                description_raw=data.get("industry"),
                source_type=source_type,
                source_provenance=provenance,
            )
            session.add(business)
            session.flush()

            # Create ProspectQualification in DISCOVERED state
            qual = ProspectQualification(
                business_id=business.id,
                qualification_state=QualificationState.DISCOVERED.value,
            )
            session.add(qual)
            session.flush()

            summary.new_businesses += 1

        except Exception as e:
            summary.invalid_quarantined += 1
            summary.errors.append(f"Row {row_idx}: {str(e)}")

    session.commit()
    return summary
