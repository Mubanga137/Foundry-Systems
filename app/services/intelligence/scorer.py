"""
Deterministic 4-Pillar Scoring Engine (0–100 Points)

Gemini provides evidence and component tags. The final score is
computed by 100% deterministic Python math — no LLM in the loop.

Pillar 1: Sector Fit         (30 pts max)
Pillar 2: Asset Footprint    (25 pts max)
Pillar 3: Digital Deficit     (25 pts max)
Pillar 4: Contact Directness (20 pts max)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
#  Sector classification sets
# --------------------------------------------------------------------------- #

TARGET_SECTORS = {
    "civil engineering", "civil earthworks", "plant hire", "quarry",
    "quarry/aggregates", "aggregates", "mining support", "heavy haulage",
    "earthmoving", "construction plant", "mining",
}

PERIPHERAL_B2B_SECTORS = {
    "commercial security", "property development", "structural engineering",
    "building construction", "electrical engineering", "mechanical engineering",
    "industrial services", "logistics",
}

NON_TARGET_KEYWORDS = {
    "retail", "bakery", "restaurant", "salon", "boutique",
    "pharmacy", "grocery", "fashion", "catering", "beauty",
    "consumer", "food", "bar", "lodge",
}


@dataclass
class ScoringResult:
    """Result of the 4-pillar scoring computation."""
    pillar_1_sector: int = 0
    pillar_2_assets: int = 0
    pillar_3_digital: int = 0
    pillar_4_contact: int = 0
    total_score: int = 0
    sector_match: bool = False
    disqualification_flags: list[str] | None = None

    @property
    def is_qualified(self) -> bool:
        return self.total_score >= 65 and self.sector_match


def _score_sector_fit(analysis: dict[str, Any]) -> tuple[int, bool, list[str]]:
    """
    Pillar 1: Sector Fit (30 pts max).

    Returns (score, sector_match, disqualification_flags).
    """
    flags = []
    primary = (analysis.get("primary_sector") or "").lower().strip()
    gemini_sector_match = analysis.get("sector_match", False)

    # Check against target sectors
    for target in TARGET_SECTORS:
        if target in primary:
            return 30, True, flags

    # Check against peripheral B2B
    for peripheral in PERIPHERAL_B2B_SECTORS:
        if peripheral in primary:
            return 15, True, flags

    # Check for non-target keywords
    for keyword in NON_TARGET_KEYWORDS:
        if keyword in primary:
            flags.append(f"Non-target sector: {primary}")
            return 0, False, flags

    # Unknown sector — use Gemini's sector_match hint
    if gemini_sector_match:
        return 15, True, flags

    flags.append(f"Unrecognized sector: {primary}")
    return 0, False, flags


def _score_asset_footprint(analysis: dict[str, Any]) -> int:
    """
    Pillar 2: Asset Footprint (25 pts max).
    """
    assets = analysis.get("asset_footprint", [])
    if not assets:
        return 0

    # Heavy yellow iron / haulage / quarry indicators
    heavy_keywords = {
        "excavator", "tipper", "loader", "bulldozer", "grader",
        "crane", "crusher", "roller", "dump truck", "haul truck",
        "quarry", "plant", "fleet", "earthmov", "cat ", "komatsu",
        "volvo", "jcb", "bell", "tipping",
    }

    # General industrial indicators
    general_keywords = {
        "depot", "warehouse", "workshop", "yard", "equipment",
        "machinery", "tools", "vehicle", "truck", "industrial",
    }

    asset_text = " ".join(str(a).lower() for a in assets)

    for keyword in heavy_keywords:
        if keyword in asset_text:
            return 25

    for keyword in general_keywords:
        if keyword in asset_text:
            return 15

    # Has assets but type unknown — moderate credit
    return 10 if assets else 0


def _score_digital_deficit(
    analysis: dict[str, Any],
    has_website: bool,
) -> tuple[int, list[str]]:
    """
    Pillar 3: Digital Deficit (25 pts max).

    Higher score = greater opportunity for Foundry.
    """
    flags = []
    deficits = analysis.get("digital_deficits", [])
    deficit_text = " ".join(str(d).lower() for d in deficits)

    # Fully modernized — no opportunity
    modern_keywords = {"responsive", "modern portal", "active rfq", "fully functional"}
    for kw in modern_keywords:
        if kw in deficit_text:
            flags.append("Fully modernized digital presence — no deficit")
            return 0, flags

    if not has_website:
        return 25, flags

    # Has website but deficient
    deficit_indicators = {"no mobile", "broken", "inactive", "outdated", "missing"}
    for indicator in deficit_indicators:
        if indicator in deficit_text:
            return 25, flags

    # Basic website present but lacks depth
    if deficits:
        return 15, flags

    # Has website, no deficits identified — moderate opportunity
    return 5, flags


def _score_contact_directness(
    has_fb_messenger: bool,
    phone_normalized: str | None,
) -> int:
    """
    Pillar 4: Contact Directness (20 pts max).
    """
    if has_fb_messenger:
        return 20

    if phone_normalized and phone_normalized.startswith("+260"):
        # Check if mobile (9X or 7X after +260)
        subscriber = phone_normalized[4:]
        if subscriber and subscriber[0] in ("9", "7"):
            return 15
        else:
            # Landline
            return 5

    return 5  # Unverified contact


def compute_score(
    analysis: dict[str, Any],
    has_website: bool = False,
    has_fb_messenger: bool = False,
    phone_normalized: str | None = None,
) -> ScoringResult:
    """
    Compute the deterministic 4-pillar score for a business.

    Args:
        analysis: Gemini analysis output dict.
        has_website: Whether the business has a website URL.
        has_fb_messenger: Whether a Facebook Messenger endpoint exists.
        phone_normalized: E.164 phone number or None.

    Returns:
        ScoringResult with individual pillar scores and qualification flag.
    """
    flags = []

    p1_score, sector_match, p1_flags = _score_sector_fit(analysis)
    flags.extend(p1_flags)

    p2_score = _score_asset_footprint(analysis)

    p3_score, p3_flags = _score_digital_deficit(analysis, has_website)
    flags.extend(p3_flags)

    p4_score = _score_contact_directness(has_fb_messenger, phone_normalized)

    total = p1_score + p2_score + p3_score + p4_score

    return ScoringResult(
        pillar_1_sector=p1_score,
        pillar_2_assets=p2_score,
        pillar_3_digital=p3_score,
        pillar_4_contact=p4_score,
        total_score=total,
        sector_match=sector_match,
        disqualification_flags=flags if flags else None,
    )
