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

import re
import urllib.parse

from app.models.business import Business
from app.models.prospect import ProspectQualification
from app.schemas.states import QualificationState
from app.services.ingestion.normalizer import normalize_zambian_phone
from app.services.ingestion.deduplicator import find_duplicate, merge_into_existing

# --------------------------------------------------------------------------- #
#  Facebook Scraper Constants & Regexes
# --------------------------------------------------------------------------- #

FB_NOISE_TOKENS: set[str] = {
    "pages",
    "groups",
    "see more",
    "follow",
    "facebook",
    "shared with public",
    "shared post",
    "[object object]",
    "closed now",
    "open now",
    "comment as foundry systems",
    "shared with public group",
    "photos from",
    "no photo description available.",
    "no photo description available",
    "whatsapp group invite",
}

COMMERCIAL_IDENTIFIERS: list[str] = [
    "contractor",
    "contractors",
    "construction",
    "limited",
    "ltd",
    "earth work",
    "engineering",
    "supplies",
    "glass",
    "builders",
    "builder",
    "electrical",
    "services",
    "logistics",
    "civil",
    "quarry",
    "haulage",
    "company",
]

ZAMBIA_PHONE_REGEX = re.compile(r"(?:(?:\+?260)|0)[79][567]\d{7}")
WA_REGEX = re.compile(r"(?:api\.whatsapp\.com/send\?phone=|wa\.me/)(\d{10,13})")


def is_facebook_scraper_export(headers: list[str]) -> bool:
    """Detect if file headers contain minified Facebook CSS classes."""
    for h in headers:
        h_clean = h.strip().lower()
        if (
            h_clean.startswith("x1i10hfl")
            or h_clean.startswith("x1lliihq")
            or h_clean.startswith("xdj266r")
            or "html-span" in h_clean
        ):
            return True
    return False


def clean_facebook_url(raw_url: Optional[str]) -> Optional[str]:
    """Clean Facebook page/profile URLs and strip tracking query parameters."""
    if not raw_url or not isinstance(raw_url, str):
        return None
    raw_url = raw_url.strip()
    if not ("facebook.com" in raw_url or "fb.com" in raw_url):
        return None
    low = raw_url.lower()
    # Ignore search, photo modals, stories, and l.facebook redirector
    if (
        "/search/" in low
        or "/photo/" in low
        or "/photo.php" in low
        or "l.facebook.com" in low
        or "/stories/" in low
    ):
        return None
    # Ignore general group feed URLs without specific user context
    if "/groups/" in low and "/user/" not in low:
        return None

    try:
        parsed = urllib.parse.urlparse(raw_url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or "web.facebook.com"

        if "profile.php" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            if "id" in qs:
                page_id = qs["id"][0]
                return f"{scheme}://{netloc}{parsed.path}?id={page_id}"
            return None

        if "/groups/" in parsed.path and "/user/" in parsed.path:
            path = parsed.path.rstrip("/")
            return f"{scheme}://{netloc}{path}"

        path = parsed.path.rstrip("/")
        if not path or path in ("", "/"):
            return None

        return f"{scheme}://{netloc}{path}"
    except Exception:
        return None


def is_valid_company_name_candidate(val: str) -> bool:
    """Validate whether candidate text qualifies as a business name."""
    if not val or not isinstance(val, str):
        return False
    val = val.strip()
    v_clean = val.lower()
    if v_clean in FB_NOISE_TOKENS:
        return False
    for noise in FB_NOISE_TOKENS:
        if v_clean.startswith(noise):
            return False
    if len(val) < 3 or len(val) > 75:
        return False
    if val.startswith("http://") or val.startswith("https://") or val.startswith("data:"):
        return False
    if val.startswith("·") or val == "·":
        return False
    if re.match(r"^[kK]?[\d,]+(?:\.\d+)?[kKmM]?$", val):
        return False
    if val.startswith("**admin") or val.startswith("admin "):
        return False
    if len(re.findall(r"[a-zA-Z]", val)) < 2:
        return False
    return True


def extract_zambian_phones_from_row(row: list[str]) -> list[str]:
    """Scan all cells in a row for Zambian mobile and WhatsApp numbers."""
    phones: list[str] = []
    for cell in row:
        if not cell or not isinstance(cell, str):
            continue
        # Check WhatsApp link format
        for wa_num in WA_REGEX.findall(cell):
            norm = normalize_zambian_phone(wa_num)
            if norm and norm not in phones:
                phones.append(norm)
        # Check text regex
        for ph in ZAMBIA_PHONE_REGEX.findall(cell):
            norm = normalize_zambian_phone(ph)
            if norm and norm not in phones:
                phones.append(norm)
    return phones


def assemble_facebook_description(
    row: list[str], headers: list[str], extra_phones: list[str]
) -> Optional[str]:
    """Assemble raw post description from post text columns and extra phone contacts."""
    snippets: list[str] = []
    for idx, h in enumerate(headers):
        if idx >= len(row):
            continue
        h_low = h.lower()
        if any(
            target in h_low
            for target in ("html-span 3", "html-span 4", "xdj266r 2", "xdj266r")
        ):
            val = row[idx].strip()
            if (
                val
                and val.lower() not in FB_NOISE_TOKENS
                and not any(val.lower().startswith(tok) for tok in FB_NOISE_TOKENS)
                and not val.startswith("data:")
                and not val.startswith("http")
                and val not in snippets
            ):
                snippets.append(val)
    if extra_phones:
        snippets.append("Additional contact numbers: " + ", ".join(extra_phones))
    return "\n\n".join(snippets) if snippets else None


def _import_facebook_scraper(
    session: Session,
    filepath: Path,
    headers: list[str],
    rows: list[list[str]],
    summary: ImportSummary,
    source_type: str = "FACEBOOK_INSTANT_DATA_SCRAPER",
) -> ImportSummary:
    """Adaptive Facebook Scraper parser mode."""
    summary.rows_read = len(rows)
    primary_candidate_indices = [
        i
        for i, h in enumerate(headers)
        if h.lower() in ("x1i10hfl", "xdj266r", "html-span", "html-span 2")
    ]
    secondary_candidate_indices = [
        i
        for i, h in enumerate(headers)
        if any(k in h.lower() for k in ("x193iq5w",))
        and "href" not in h.lower()
        and "src" not in h.lower()
    ]
    href_indices = [i for i, h in enumerate(headers) if "href" in h.lower()]

    for row_idx, row in enumerate(rows):
        try:
            # 1. Company Name Candidate Selection
            candidates: list[tuple[int, str]] = []
            seen_cand_vals = set()

            for c_idx in primary_candidate_indices:
                if c_idx < len(row):
                    val = row[c_idx].strip()
                    if val and val not in seen_cand_vals and is_valid_company_name_candidate(val):
                        seen_cand_vals.add(val)
                        has_comm = any(ci in val.lower() for ci in COMMERCIAL_IDENTIFIERS)
                        score = (100 if has_comm else 0) + 50 - (len(val) if len(val) > 40 else 0)
                        candidates.append((score, val))

            for c_idx in secondary_candidate_indices:
                if c_idx < len(row):
                    val = row[c_idx].strip()
                    if val and val not in seen_cand_vals and is_valid_company_name_candidate(val):
                        seen_cand_vals.add(val)
                        has_comm = any(ci in val.lower() for ci in COMMERCIAL_IDENTIFIERS)
                        score = (100 if has_comm else 0) - (len(val) if len(val) > 40 else 0)
                        candidates.append((score, val))

            if not candidates:
                summary.invalid_quarantined += 1
                continue

            candidates.sort(key=lambda x: x[0], reverse=True)
            company_name = candidates[0][1]

            summary.valid_records += 1

            # 2. Extract Facebook Page URL
            fb_page_url = None
            for h_idx in href_indices:
                if h_idx < len(row):
                    cleaned_u = clean_facebook_url(row[h_idx])
                    if cleaned_u:
                        fb_page_url = cleaned_u
                        break

            # 3. Extract Deep Zambian Phones
            all_phones = extract_zambian_phones_from_row(row)
            phone_normalized = all_phones[0] if all_phones else None
            phone_raw = phone_normalized
            extra_phones = all_phones[1:] if len(all_phones) > 1 else []

            # 4. Assemble Post Description
            description_raw = assemble_facebook_description(row, headers, extra_phones)

            # 5. Deduplication
            existing = find_duplicate(
                session,
                phone_normalized=phone_normalized,
                website_url=None,
                company_name=company_name,
                location_district=None,
            )

            if existing:
                merge_data = {
                    "phone_raw": phone_raw,
                    "phone_normalized": phone_normalized,
                    "fb_page_url": fb_page_url,
                    "industry_sector": "Construction / Contractors",
                    "description_raw": description_raw,
                }
                merged = merge_into_existing(existing, merge_data)
                if merged:
                    summary.duplicates_merged += 1
                else:
                    summary.duplicates_skipped += 1
                continue

            # 6. Source Provenance
            provenance = {
                "source_file": filepath.name,
                "parser_mode": "FACEBOOK_INSTANT_DATA_SCRAPER",
                "extraction_date": datetime.now(timezone.utc).isoformat(),
                "row_index": row_idx,
                "extracted_company": company_name,
                "extracted_phone": phone_normalized,
                "extracted_fb_url": fb_page_url,
            }

            # 7. Create Business & Qualification
            effective_source = (
                source_type
                if source_type != "INSTANT_DATA_SCRAPER"
                else "FACEBOOK_INSTANT_DATA_SCRAPER"
            )
            business = Business(
                company_name=company_name,
                phone_raw=phone_raw,
                phone_normalized=phone_normalized,
                email=None,
                website_url=None,
                fb_page_url=fb_page_url,
                industry_sector="Construction / Contractors",
                location_district=None,
                description_raw=description_raw,
                source_type=effective_source,
                source_provenance=provenance,
            )
            session.add(business)
            session.flush()

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

    - Detects and adapts to minified Facebook scraper exports.
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

    # Adaptive Facebook Scraper Mode
    if is_facebook_scraper_export(headers):
        return _import_facebook_scraper(
            session=session,
            filepath=filepath,
            headers=headers,
            rows=rows,
            summary=summary,
            source_type=source_type,
        )

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
