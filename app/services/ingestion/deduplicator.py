"""
Entity Deduplication Engine

Rules (checked in order):
  1. Exact match on non-null phone_normalized.
  2. Exact match on non-null normalized website domain.
  3. Fuzzy name match (Levenshtein ratio > 0.88) + same city/district.

When a duplicate is found, non-conflicting new information is merged
into the existing Business record without overwriting higher-confidence fields.
"""

from __future__ import annotations

import re
from typing import Optional

from Levenshtein import ratio as levenshtein_ratio
from sqlalchemy.orm import Session

from app.models.business import Business
from app.services.ingestion.normalizer import extract_domain

# Corporate suffixes to strip for name comparison
_CORP_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|inc|incorporated|co|company|corp|corporation|llc)\b",
    re.IGNORECASE,
)
_MULTI_SPACE = re.compile(r"\s+")

# Minimum Levenshtein ratio for fuzzy name match
FUZZY_NAME_THRESHOLD = 0.88


def _normalize_name(name: str) -> str:
    """Lowercase, strip corporate suffixes, collapse whitespace."""
    cleaned = _CORP_SUFFIXES.sub("", name.lower())
    cleaned = re.sub(r"[^\w\s]", "", cleaned)  # remove punctuation
    return _MULTI_SPACE.sub(" ", cleaned).strip()


def find_duplicate(
    session: Session,
    phone_normalized: Optional[str],
    website_url: Optional[str],
    company_name: str,
    location_district: Optional[str],
) -> Optional[Business]:
    """
    Search for an existing Business that matches by phone, domain, or fuzzy name.
    Returns the matching Business or None.
    """
    # Rule 1: Exact phone match
    if phone_normalized:
        match = (
            session.query(Business)
            .filter(Business.phone_normalized == phone_normalized)
            .first()
        )
        if match:
            return match

    # Rule 2: Exact domain match
    domain = extract_domain(website_url)
    if domain:
        # Check all businesses with a website and compare domains
        candidates = (
            session.query(Business)
            .filter(Business.website_url.isnot(None))
            .all()
        )
        for candidate in candidates:
            if extract_domain(candidate.website_url) == domain:
                return candidate

    # Rule 3: Fuzzy name + district match
    norm_name = _normalize_name(company_name)
    norm_district = (location_district or "").strip().lower()

    if norm_name:
        # Load candidates from same district (or all if district unknown)
        if norm_district:
            candidates = session.query(Business).filter(
                Business.location_district.isnot(None)
            ).all()
            candidates = [
                c for c in candidates
                if (c.location_district or "").strip().lower() == norm_district
            ]
        else:
            candidates = []

        for candidate in candidates:
            candidate_norm = _normalize_name(candidate.company_name)
            if levenshtein_ratio(norm_name, candidate_norm) > FUZZY_NAME_THRESHOLD:
                return candidate

    return None


def merge_into_existing(existing: Business, new_data: dict) -> bool:
    """
    Merge non-conflicting new information into an existing Business record.
    Only fills in None/empty fields — never overwrites non-null existing values.

    Returns True if any field was updated.
    """
    updated = False
    mergeable_fields = [
        "phone_raw", "phone_normalized", "email", "website_url",
        "fb_page_url", "industry_sector", "location_district", "description_raw",
    ]
    for field in mergeable_fields:
        existing_val = getattr(existing, field, None)
        new_val = new_data.get(field)
        if not existing_val and new_val:
            setattr(existing, field, new_val)
            updated = True
    return updated
