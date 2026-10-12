"""
Zambian E.164 Phone & URL Normalizers

Handles the full range of Zambian mobile prefixes:
  Airtel: 097, 077
  MTN:    096, 076
  Zamtel: 095, 075

Transforms local or international-format numbers to +260XXXXXXXXX.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


# Valid Zambian mobile network prefixes (after country code 260)
_ZAMBIAN_MOBILE_PREFIXES = {"97", "96", "95", "77", "76", "75"}
# Landline prefixes (Lusaka 211, Copperbelt 212, etc.)
_ZAMBIAN_LANDLINE_PREFIX_RE = re.compile(r"^21[1-9]")

# Strip all non-digit characters
_NON_DIGIT = re.compile(r"[^\d]")


def normalize_zambian_phone(raw: str | None) -> str | None:
    """
    Normalize a Zambian phone number to E.164 format (+260XXXXXXXXX).

    Returns None if the number cannot be resolved to a valid Zambian sequence.
    """
    if not raw or not raw.strip():
        return None

    digits = _NON_DIGIT.sub("", raw.strip())

    if not digits:
        return None

    # Strip leading '+' artifacts (already removed by regex, but guard)
    # Handle international prefix: 00260... → 260...
    if digits.startswith("00260"):
        digits = digits[2:]  # drop "00"

    # Handle 260XXXXXXXXX (with or without leading +)
    if digits.startswith("260") and len(digits) >= 12:
        subscriber = digits[3:]  # after "260"
        prefix2 = subscriber[:2]
        if prefix2 in _ZAMBIAN_MOBILE_PREFIXES and len(subscriber) == 9:
            return f"+260{subscriber}"
        # Landline: 260 21X XXXXXXX
        if _ZAMBIAN_LANDLINE_PREFIX_RE.match(subscriber) and len(subscriber) >= 9:
            return f"+260{subscriber[:9]}"
        return None

    # Handle local format: 0XX XXXXXXX → 260 XX XXXXXXX
    if digits.startswith("0") and len(digits) >= 10:
        subscriber = digits[1:]  # drop leading 0
        prefix2 = subscriber[:2]
        if prefix2 in _ZAMBIAN_MOBILE_PREFIXES and len(subscriber) == 9:
            return f"+260{subscriber}"
        if _ZAMBIAN_LANDLINE_PREFIX_RE.match(subscriber) and len(subscriber) >= 9:
            return f"+260{subscriber[:9]}"
        return None

    # Raw digits that don't match any pattern
    return None


def normalize_url(raw: str | None) -> str | None:
    """
    Normalize a URL to a canonical lowercase form.
    Returns None if input is empty or clearly not a URL.
    """
    if not raw or not raw.strip():
        return None

    url = raw.strip().lower()

    # Ensure scheme is present for urlparse
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        # Return just the hostname (domain) for dedup matching
        return parsed.hostname.removeprefix("www.")
    except Exception:
        return None


def extract_domain(url: str | None) -> str | None:
    """Extract the bare domain from a URL for deduplication."""
    return normalize_url(url)
