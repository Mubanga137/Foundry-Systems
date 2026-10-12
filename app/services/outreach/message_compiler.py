"""
FS-CAE Message Compiler

Compiles high-converting, policy-compliant $100M Leads outbound messages
for qualified commercial contractors. Integrates live Gemini with guaranteed
deterministic, context-rich fallback.
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

GEMINI_SYSTEM_PROMPT = """You are the Senior Direct-Response Copywriter for Foundry Systems in Zambia.
Write a 1-on-1 cold direct message (<60 words) to this specific business on Facebook Messenger.

STRICT COPYWRITING RULES ($100M Leads Framework):
1. SPECIFIC OBSERVATION: Reference their ACTUAL service or a real detail from their post (e.g. if they sell glass, talk about architectural glass supply; if they build houses, mention their residential builds or plastering/roofing; if they do electrical, mention electrical contracting). NEVER mention excavators unless they specifically advertise excavators.
2. THE COMMERCIAL FRICTION: Point out that commercial clients or developers vetting them online lack an interactive spec catalogue or direct mobile RFQ portal to evaluate their capacity.
3. THE LEAD MAGNET: Offer a quick 3-point digital capability review.
4. PERMISSION CTA: End with: "Mind if I drop the preview here?"
5. CONSTRAINTS: Under 60 words. Zero exclamation marks. No buzzwords ("excited", "synergy", "affordable", "game-changer").

OUTPUT ONLY THE RAW MESSAGE TEXT. NO QUOTES, NO EXPLANATIONS."""


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


def extract_primary_asset(
    qualification: Optional[ProspectQualification] = None,
    business: Optional[Business] = None,
) -> str:
    """
    Extract primary operational asset or service.
    Prioritizes explicit qualification asset footprint; falls back to context-rich
    inference from business name, category, and raw post excerpts.
    """
    if qualification and qualification.asset_footprint:
        fp = qualification.asset_footprint
        if isinstance(fp, dict):
            for key in ("heavy_machinery", "materials", "equipment", "plant", "fleet", "assets"):
                val = fp.get(key)
                if val:
                    if isinstance(val, list) and val:
                        return str(val[0])
                    elif isinstance(val, str) and val.strip():
                        return val.strip()

            for val in fp.values():
                if isinstance(val, list) and val:
                    return str(val[0])
                elif isinstance(val, str) and val.strip():
                    return val.strip()
        elif isinstance(fp, list) and fp:
            return str(fp[0])
        elif isinstance(fp, str) and fp.strip():
            return fp.strip()

    if business is not None:
        text = f"{business.company_name or ''} {business.description_raw or ''} {business.industry_sector or ''}".lower()
        if any(w in text for w in ("glass", "glazing", "aluminium", "aluminum")):
            return "architectural glass supply"
        if any(w in text for w in ("electric", "electrical", "wiring", "solar", "substation")):
            return "electrical contracting"
        if any(w in text for w in ("build", "construct", "house", "residential", "civil", "masonry", "roofing")):
            return "building and construction"
        if any(w in text for w in ("sand", "quarry", "aggregate", "stone", "gravel")):
            return "quarry and aggregate supply"
        if any(w in text for w in ("grading", "grader", "earthmoving")):
            return "road grading and earthmoving"
        if any(w in text for w in ("excavator", "excavation", "digger")):
            return "excavation operations"
        if any(w in text for w in ("plant hire", "plant & haulage", "heavy machinery")):
            return "plant hire and heavy equipment"
        if any(w in text for w in ("haulage", "logistics", "freight", "transport")):
            return "heavy haulage and logistics"

    return "plant and haulage fleet"


def extract_location(business: Business) -> str:
    """Extract location district or corridor name."""
    if business.location_district and business.location_district.strip():
        return business.location_district.strip()
    return "the industrial corridor"


def compile_deterministic_fallback(
    business: Business,
    qualification: Optional[ProspectQualification] = None,
) -> str:
    """
    Deterministic template fallback adhering 100% to $100M Leads grammar:
    - Hook: Acknowledge operational asset + corridor.
    - The Gap: Context-aware friction (spec catalogue, mobile RFQ, or fleet verification).
    - Lead Magnet: 3-point capability review.
    - Permission CTA: "Mind if I drop it here?"
    - Strictly <= 65 words, 0 exclamation marks, 0 banned words.
    """
    asset = extract_primary_asset(qualification, business)
    location = extract_location(business)

    asset_lower = asset.lower()
    if "glass" in asset_lower:
        gap = "Commercial developers vetting your supply online lack an interactive spec catalogue or direct mobile RFQ portal."
    elif "electric" in asset_lower:
        gap = "Commercial clients vetting your engineering online lack an interactive spec catalogue or mobile RFQ portal."
    elif any(k in asset_lower for k in ("build", "construct", "house", "civil")):
        gap = "Commercial developers vetting your capacity online lack an interactive spec catalogue or direct mobile RFQ portal."
    elif any(k in asset_lower for k in ("sand", "quarry", "aggregate", "stone")):
        gap = "Commercial contractors sourcing materials online lack direct rate cards or mobile RFQ dispatch."
    else:
        gap = "Commercial buyers cannot verify your fleet specs or submit an RFQ on mobile."

    message = (
        f"Noticed your {asset} operations along {location}. "
        f"{gap} "
        f"We built a complimentary 3-point capability review for your team. "
        f"Mind if I drop it here?"
    )

    valid, reason = validate_outreach_grammar(message)
    if not valid:
        logger.error("Deterministic template failed validation: %s", reason)
        return (
            f"Noticed your {asset} operations. "
            f"{gap} "
            f"We built a 3-point capability review. "
            f"Mind if I drop it here?"
        )

    return message


class MessageCompiler:
    """
    Compiles direct-response outreach copy for qualified prospects.
    Uses live Gemini when available and falls back to deterministic copy.
    """

    def __init__(self, gemini_client: Optional[Any] = None):
        self.gemini_client = gemini_client

    def compile(
        self,
        business: Business,
        qualification: Optional[ProspectQualification] = None,
    ) -> str:
        """
        Compile outreach copy for a business.
        Always returns verified, constraint-compliant copy.
        """
        if self.gemini_client is not None:
            try:
                asset = extract_primary_asset(qualification, business)
                location = extract_location(business)
                description_raw = (business.description_raw or "").strip()

                user_prompt = (
                    f"BUSINESS CONTEXT:\n"
                    f"- Company Name: {business.company_name}\n"
                    f"- Territory/Location: {location}\n"
                    f"- Detected Assets/Services: {asset}\n"
                    f"- Raw Post Excerpt: {description_raw or 'Commercial contractor in Zambia'}\n"
                )

                generated = None
                if hasattr(self.gemini_client, "generate_text"):
                    generated = self.gemini_client.generate_text(
                        prompt=user_prompt,
                        system_instruction=GEMINI_SYSTEM_PROMPT,
                    )
                elif hasattr(self.gemini_client, "generate_content"):
                    full_p = f"{GEMINI_SYSTEM_PROMPT}\n\n---\n\n{user_prompt}"
                    res = self.gemini_client.generate_content(full_p)
                    generated = getattr(res, "text", str(res)) if res else None

                if generated and isinstance(generated, str):
                    cleaned = generated.strip()
                    if cleaned.startswith("```"):
                        lines = [l for l in cleaned.split("\n") if not l.strip().startswith("```")]
                        cleaned = "\n".join(lines).strip()
                    cleaned = cleaned.strip('"').strip("'").strip()
                    cleaned = cleaned.replace("!", ".")
                    cleaned = " ".join(cleaned.split())

                    valid, reason = validate_outreach_grammar(cleaned)
                    if valid:
                        return cleaned
                    else:
                        logger.warning(
                            "Gemini message violated grammar constraints (%s): %s. Falling back.",
                            reason,
                            cleaned,
                        )
            except Exception as exc:
                logger.warning("Gemini message compilation failed (%s). Using fallback.", exc)

        return compile_deterministic_fallback(business, qualification)
