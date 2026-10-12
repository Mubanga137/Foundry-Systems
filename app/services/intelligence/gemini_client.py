"""
Gemini Analysis Client

Wraps the Google Generative AI SDK to perform structured B2B analysis
of Zambian businesses. Outputs strict JSON conforming to the analysis schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.services.intelligence.key_manager import GeminiKeyManager, redact_key

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  System prompt for Gemini
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are an objective B2B industrial analyst specializing in Zambian commercial markets.
You will receive public information about a Zambian business. Analyze ONLY the provided text.

STRICT RULES:
- Do NOT invent fleet sizes, revenue figures, or executive names.
- Do NOT assume capabilities beyond what is explicitly stated.
- Base your analysis solely on the evidence provided.

Respond with ONLY valid JSON matching this exact schema:
{
  "sector_match": boolean,
  "primary_sector": "Civil Engineering | Plant Hire | Quarry | Mining Support | Heavy Haulage | Commercial Security | Property Development | Structural Engineering | Retail | Other",
  "asset_footprint": ["list of confirmed physical assets or equipment mentioned"],
  "digital_deficits": ["list of identified digital gaps or missing online capabilities"],
  "evidence_summary": "Brief factual summary of key findings from the provided text",
  "confidence_score": float between 0.0 and 1.0
}"""


def _build_analysis_prompt(business_data: dict) -> str:
    """Build the user prompt from business data fields."""
    parts = [f"Company: {business_data.get('company_name', 'Unknown')}"]

    if business_data.get("industry_sector"):
        parts.append(f"Industry/Category: {business_data['industry_sector']}")
    if business_data.get("description_raw"):
        parts.append(f"Description: {business_data['description_raw']}")
    if business_data.get("location_district"):
        parts.append(f"Location: {business_data['location_district']}")
    if business_data.get("website_url"):
        parts.append(f"Website: {business_data['website_url']}")
    if business_data.get("fb_page_url"):
        parts.append(f"Facebook: {business_data['fb_page_url']}")
    if business_data.get("phone_normalized"):
        parts.append(f"Phone: {business_data['phone_normalized']}")

    return "\n".join(parts)


def _parse_gemini_response(text: str) -> dict[str, Any]:
    """
    Parse Gemini's JSON response, handling potential markdown code fences.
    Returns a validated dict or raises ValueError.
    """
    cleaned = text.strip()
    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned invalid JSON: {e}\nRaw: {text[:500]}")

    # Validate required fields
    required = {"sector_match", "primary_sector", "asset_footprint",
                "digital_deficits", "evidence_summary", "confidence_score"}
    missing = required - set(result.keys())
    if missing:
        raise ValueError(f"Gemini response missing fields: {missing}")

    return result


class GeminiAnalysisClient:
    """
    Client for running Gemini analysis on business data.

    Can operate with a real Google GenAI backend or with an injected
    callable for testing (analyze_fn).
    """

    def __init__(
        self,
        key_manager: GeminiKeyManager,
        model_name: str = "gemini-1.5-flash",
        analyze_fn: Optional[callable] = None,
    ):
        self._key_manager = key_manager
        self._model_name = model_name
        self._analyze_fn = analyze_fn  # For test injection

    def analyze(self, business_data: dict) -> dict[str, Any]:
        """
        Analyze a business using Gemini and return structured results.

        Args:
            business_data: Dict with keys matching Business model fields.

        Returns:
            Parsed analysis dict with sector_match, asset_footprint, etc.

        Raises:
            RuntimeError: If all keys exhausted or analysis fails.
        """
        prompt = _build_analysis_prompt(business_data)

        # Testable injection point
        if self._analyze_fn is not None:
            return self._analyze_fn(prompt)

        return self._call_gemini(prompt)

    def analyze_business(self, business_data: dict) -> dict[str, Any]:
        """Analyze a business using Gemini (alias for analyze)."""
        return self.analyze(business_data)

    def _call_gemini(self, prompt: str, max_retries: int = 3) -> dict[str, Any]:
        """Call Gemini API with key rotation, model fallback, and retry logic."""
        import google.generativeai as genai

        last_error = None

        for attempt in range(max_retries):
            key = self._key_manager.acquire_key()
            try:
                genai.configure(api_key=key)

                candidate_models = [self._model_name]
                for fallback in ("gemini-1.5-flash-latest", "gemini-flash-latest"):
                    if fallback not in candidate_models:
                        candidate_models.append(fallback)

                response = None
                model_err = None
                for m_name in candidate_models:
                    try:
                        model = genai.GenerativeModel(m_name)
                        response = model.generate_content(
                            f"{SYSTEM_PROMPT}\n\n---\n\n{prompt}"
                        )
                        break
                    except Exception as me:
                        m_err_str = str(me)
                        if "404" in m_err_str and ("not found" in m_err_str or "no longer available" in m_err_str):
                            model_err = me
                            continue
                        raise me

                if response is None and model_err:
                    raise model_err

                self._key_manager.release_key(key, success=True)
                return _parse_gemini_response(response.text)

            except Exception as e:
                error_str = str(e)
                error_code = None
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    error_code = 429
                elif "401" in error_str or "403" in error_str:
                    error_code = 401
                self._key_manager.release_key(
                    key, success=False, error_code=error_code
                )
                last_error = e
                logger.warning(
                    "Gemini call failed (attempt %d/%d) with key %s: %s",
                    attempt + 1, max_retries, redact_key(key), error_str,
                )

        raise RuntimeError(
            f"Gemini analysis failed after {max_retries} attempts: {last_error}"
        )


# Export alias
GeminiClient = GeminiAnalysisClient
