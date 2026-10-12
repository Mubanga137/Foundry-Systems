"""
FS-CAE Message Compiler

Compiles high-converting, policy-compliant $100M Leads outbound messages
for qualified commercial contractors. Integrates Gemini with guaranteed
deterministic fallback.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from app.models.business import Business
from app.models.prospect import ProspectQualification

logger = logging.getLogger(__name__)

BANNED_PHRASES = [
    "excited",
    "synergy",
    "hope you are well",
    "game-changer",
    "game changer",
    "revolutionize",
    "revolutionise",
    "affordable",
]

MAX_WORD_COUNT = 65


def validate_outreach_grammar(text: str) -> tuple[bool, Optional[str]]:
    """
    Validate message against strict $100M Leads direct-response constraints.

    Returns:
        (is_valid, failure_reason)
    """
    if not text or not text.strip():
        return False, "Message is empty"

    if "!" in text:
        return False, "Message contains exclamation mark (!)"

    word_count = len(text.strip().split())
    if word_count > MAX_WORD_COUNT:
        return False, f"Message exceeds maximum {MAX_WORD_COUNT} words ({word_count} words)"

    text_lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in text_lower:
            return False, f"Message contains banned buzzword/phrase: '{phrase}'"

    return True, None


def extract_primary_asset(qualification: Optional[ProspectQualification]) -> str:
    """Extract primary operational asset from prospect qualification evidence."""
    if not qualification or not qualification.asset_footprint:
        return "plant and haulage fleet"

    fp = qualification.asset_footprint
    if isinstance(fp, dict):
        # Check standard asset footprint categories
        for key in ("heavy_machinery", "materials", "equipment", "plant", "fleet", "assets"):
            val = fp.get(key)
            if val:
                if isinstance(val, list) and val:
                    return str(val[0])
                elif isinstance(val, str) and val.strip():
                    return val.strip()

        # Check any values
        for val in fp.values():
            if isinstance(val, list) and val:
                return str(val[0])
            elif isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(fp, list) and fp:
        return str(fp[0])
    elif isinstance(fp, str) and fp.strip():
        return fp.strip()

    return "plant and haulage fleet"


def extract_location(business: Business) -> str:
    """Extract location district or corridor name."""
    if business.location_district and business.location_district.strip():
        return business.location_district.strip()
    return "the industrial corridor"


def compile_deterministic_fallback(
    business: Business,
    qualification: Optional[ProspectQualification],
) -> str:
    """
    Deterministic template fallback adhering 100% to $100M Leads grammar:
    - Hook: Acknowledge operational asset + corridor.
    - The Gap: Procurement teams / buyers cannot verify fleet specs or submit RFQ on mobile.
    - Lead Magnet: 3-point capability review.
    - Permission CTA: "Mind if I drop it here?"
    - Strictly <= 65 words, 0 exclamation marks, 0 banned words.
    """
    asset = extract_primary_asset(qualification)
    location = extract_location(business)

    message = (
        f"Noticed your {asset} operations along {location}. "
        f"Commercial buyers cannot verify your fleet specs or submit an RFQ on mobile. "
        f"We built a complimentary 3-point capability review for your team. "
        f"Mind if I drop it here?"
    )

    valid, reason = validate_outreach_grammar(message)
    if not valid:
        logger.error("Deterministic template failed validation: %s", reason)
        # Ultra-safe bare fallback
        return (
            f"Noticed your {asset} operations. "
            f"Buyers cannot verify your fleet specs on mobile. "
            f"We built a 3-point capability review. "
            f"Mind if I drop it here?"
        )

    return message


class MessageCompiler:
    """
    Compiles direct-response outreach copy for qualified prospects.
    Uses Gemini when available and falls back to deterministic copy.
    """

    def __init__(self, gemini_client: Optional[Any] = None):
        self.gemini_client = gemini_client

    def compile(
        self,
        business: Business,
        qualification: Optional[ProspectQualification],
    ) -> str:
        """
        Compile outreach copy for a business.
        Always returns verified, constraint-compliant copy.
        """
        if self.gemini_client is not None:
            try:
                asset = extract_primary_asset(qualification)
                location = extract_location(business)
                prompt = (
                    f"Write a 1-paragraph outreach direct message for {business.company_name}.\n"
                    f"Operational asset: {asset}\n"
                    f"Corridor/location: {location}\n"
                    f"Industry sector: {business.industry_sector or 'Contracting'}\n\n"
                    f"Requirements:\n"
                    f"- Hook acknowledging their {asset} along {location}\n"
                    f"- The Gap: commercial buyers or procurement cannot verify fleet specs or submit an RFQ on mobile\n"
                    f"- Lead Magnet: offer a complimentary 3-point capability review\n"
                    f"- Permission CTA: end with 'Mind if I drop it here?'\n"
                    f"- Strictly under 50 words\n"
                    f"- ZERO exclamation marks\n"
                    f"- NO banned words: Excited, Synergy, Hope you are well, Game-changer, Revolutionize, Affordable\n"
                    f"Output ONLY the text."
                )

                # Attempt Gemini generation if client has generate_text or similar
                generated = None
                if hasattr(self.gemini_client, "generate_text"):
                    generated = self.gemini_client.generate_text(prompt)
                elif hasattr(self.gemini_client, "generate_content"):
                    res = self.gemini_client.generate_content(prompt)
                    generated = getattr(res, "text", str(res))

                if generated and isinstance(generated, str):
                    # Clean up quotes and newlines
                    cleaned = generated.strip().strip('"').strip("'").replace("!", ".")
                    valid, reason = validate_outreach_grammar(cleaned)
                    if valid:
                        return cleaned
                    else:
                        logger.warning(
                            "Gemini message violated grammar constraints (%s). Falling back.",
                            reason,
                        )
            except Exception as exc:
                logger.warning("Gemini message compilation failed (%s). Using fallback.", exc)

        return compile_deterministic_fallback(business, qualification)
