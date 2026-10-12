"""
FS-CAE Inbound Intent Classifier

Accurately classifies incoming prospect messages using deterministic regex/keywords
first (with prioritized opt-out safeguards), falling back to Gemini for nuanced inquiries.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.schemas.states import InboundIntent

logger = logging.getLogger(__name__)

# Re-export InboundIntent for convenience
__all__ = ["InboundIntent", "classify_inbound_intent", "IntentClassifier"]

# High-confidence Regex Patterns
OPT_OUT_PATTERNS = [
    r"\b(stop|unsubscribe|remove\s+me|remove\s+us|not\s+interested|don'?t\s+message(\s+again)?|don'?t\s+contact|leave\s+me\s+alone|cancel|no\s+thanks|wrong\s+number)\b",
    r"^\s*no\s*$",
]

POSITIVE_INTEREST_PATTERNS = [
    r"\b(yes(\s+please)?|sure|go\s+ahead|send\s+it|i'?d\s+like\s+to\s+see|please\s+send|please\s+share|sounds\s+good|share\s+it|send\s+through|send\s+the\s+review|drop\s+it\s+here|ok\s+send|okay\s+send)\b",
    r"^\s*(yes|sure|ok|okay)\s*$",
]

REQUEST_PRICING_PATTERNS = [
    r"\b(how\s+much(\s+do\s+you\s+charge)?|what'?s\s+the\s+cost|send\s+pricing|what\s+are\s+your\s+rates|pricing|price|quotation|quote|rate\s+card|how\s+much\s+is\s+this|what\s+does\s+it\s+cost)\b",
]

HAS_EXISTING_WEBSITE_PATTERNS = [
    r"\b(already\s+have(\s+a)?\s+website|we\s+have\s+a\s+website|already\s+got\s+a\s+site|check\s+our\s+site|our\s+website\s+is|visit\s+our\s+website|existing\s+website|already\s+online)\b",
]

CALL_OR_MEETING_REQUEST_PATTERNS = [
    r"\b(call\s+me(\s+on)?|can\s+we\s+meet|meet\s+at\s+our\s+office|give\s+me\s+a\s+call|reach\s+me\s+on\s+phone|phone\s+me|ring\s+me|come\s+to\s+our\s+office|set\s+up\s+a\s+meeting)\b",
]

TIMING_DELAY_PATTERNS = [
    r"\b(contact\s+me\s+next\s+month|busy\s+right\s+now|check\s+back\s+later|not\s+now|call\s+back\s+next\s+week|next\s+quarter|follow\s+up\s+later|touch\s+base\s+later)\b",
]


def match_any_pattern(text: str, patterns: list[str]) -> bool:
    """Check if text matches any regex in list (case-insensitive)."""
    clean_text = text.strip().lower()
    for pattern in patterns:
        if re.search(pattern, clean_text, re.IGNORECASE):
            return True
    return False


def classify_inbound_intent(
    text: str,
    gemini_client: Optional[Any] = None,
) -> InboundIntent:
    """
    Classify inbound text into one of the InboundIntent states.
    Uses strict deterministic matching first, falling back to LLM if ambiguous.
    """
    if not text or not text.strip():
        return InboundIntent.GENERAL_QUESTION

    # 1. OPT_OUT takes priority over all other intents (Strict Compliance)
    if match_any_pattern(text, OPT_OUT_PATTERNS):
        return InboundIntent.OPT_OUT

    # 2. HAS_EXISTING_WEBSITE
    if match_any_pattern(text, HAS_EXISTING_WEBSITE_PATTERNS):
        return InboundIntent.HAS_EXISTING_WEBSITE

    # 3. REQUEST_PRICING
    if match_any_pattern(text, REQUEST_PRICING_PATTERNS):
        return InboundIntent.REQUEST_PRICING

    # 4. CALL_OR_MEETING_REQUEST
    if match_any_pattern(text, CALL_OR_MEETING_REQUEST_PATTERNS):
        return InboundIntent.CALL_OR_MEETING_REQUEST

    # 5. TIMING_DELAY
    if match_any_pattern(text, TIMING_DELAY_PATTERNS):
        return InboundIntent.TIMING_DELAY

    # 6. POSITIVE_INTEREST
    if match_any_pattern(text, POSITIVE_INTEREST_PATTERNS):
        return InboundIntent.POSITIVE_INTEREST

    # 7. Fallback to Gemini if provided and available
    if gemini_client is not None:
        try:
            prompt = (
                "You are an intent classifier for a B2B sales system in Zambia. "
                "Classify the following inbound message into exactly one of these categories:\n"
                "- POSITIVE_INTEREST (expresses agreement or willingness to receive the capability review/information)\n"
                "- REQUEST_PRICING (asks for prices, costs, rates, quotation)\n"
                "- HAS_EXISTING_WEBSITE (mentions they already have a website or online presence)\n"
                "- CALL_OR_MEETING_REQUEST (requests a phone call, in-person meeting, or office visit)\n"
                "- TIMING_DELAY (asks to follow up later, busy, next month, etc.)\n"
                "- OPT_OUT (asks to stop, unsubscribe, remove, not interested)\n"
                "- GENERAL_QUESTION (any other commercial question)\n\n"
                f"Message: \"{text}\"\n\n"
                "Respond with ONLY the category name."
            )
            raw_response = None
            if hasattr(gemini_client, "generate_text"):
                raw_response = gemini_client.generate_text(prompt)
            elif hasattr(gemini_client, "generate_content"):
                res = gemini_client.generate_content(prompt)
                raw_response = getattr(res, "text", str(res))

            if raw_response and isinstance(raw_response, str):
                cleaned = raw_response.strip().upper()
                for member in InboundIntent:
                    if member.value in cleaned:
                        return member
        except Exception as exc:
            logger.warning("Gemini intent classification fallback failed: %s", exc)

    # 8. Safe default
    return InboundIntent.GENERAL_QUESTION


class IntentClassifier:
    """Class wrapper for intent classification."""

    def __init__(self, gemini_client: Optional[Any] = None):
        self.gemini_client = gemini_client

    def classify(self, text: str) -> InboundIntent:
        return classify_inbound_intent(text, self.gemini_client)
