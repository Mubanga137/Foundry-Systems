"""
FS-CAE Live Gemini API Verification Service

Provides live health-checks, round-robin rotation validation, and smoke tests
for Google Gemini API keys while strictly enforcing credential redaction.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any, Dict, List

from app.config import get_settings
from app.services.intelligence.gemini_client import GeminiAnalysisClient, GeminiClient
from app.services.intelligence.key_manager import GeminiKeyManager, redact_key

logger = logging.getLogger(__name__)


def ping_gemini_key(key: str, model_name: str = "gemini-1.5-flash") -> Dict[str, Any]:
    """
    Perform an isolated ping test with a single Gemini API key.
    Returns status dict with pass/fail/warn and details.
    """
    import google.generativeai as genai

    genai.configure(api_key=key)

    candidate_models = [model_name]
    for fallback in ("gemini-3.8-flash", "gemini-flash-latest"):
        if fallback not in candidate_models:
            candidate_models.append(fallback)

    last_error = None
    for m in candidate_models:
        try:
            model = genai.GenerativeModel(m)
            resp = model.generate_content("ping")
            text = (resp.text or "").strip().encode("ascii", "replace").decode("ascii")
            return {
                "status": "PASS",
                "code": 200,
                "model": m,
                "message": f"HTTP 200 - Model active and responsive ({text[:20]})",
            }
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                return {
                    "status": "WARN",
                    "code": 429,
                    "model": m,
                    "message": "429 Quota Exceeded / Cooldown active",
                }
            elif "401" in err_str or "403" in err_str or "API_KEY_INVALID" in err_str:
                return {
                    "status": "FAIL",
                    "code": 401,
                    "model": m,
                    "message": "Authentication Failed - Invalid API Key",
                }
            elif "404" in err_str and ("not found" in err_str or "no longer available" in err_str):
                last_error = e
                continue
            else:
                last_error = e
                break

    return {
        "status": "FAIL",
        "code": 500,
        "model": model_name,
        "message": f"Error: {last_error}",
    }


def run_gemini_live_verification() -> bool:
    """
    Execute the complete 5-step live Gemini verification pipeline:
      1. Inspect configured keys
      2. Ping health-check on each key
      3. KeyManager round-robin rotation test
      4. Live contractor smoke analysis
      5. Credential redaction security assertion
    """
    settings = get_settings()
    raw_keys = settings.GEMINI_API_KEYS

    output_lines: List[str] = []

    def log_print(msg: str):
        output_lines.append(msg)
        enc = sys.stdout.encoding or "utf-8"
        safe_msg = msg.encode(enc, errors="replace").decode(enc)
        print(safe_msg, flush=True)

    log_print(f"\n{'='*65}")
    log_print("  FOUNDRY SYSTEMS — LIVE GEMINI API VERIFICATION")
    log_print(f"{'='*65}")

    # Step 1: Inspect Configured Keys
    log_print(f"\n[1/5] INSPECTING CONFIGURED KEYS:")
    if not raw_keys:
        log_print("  [ERROR] No Gemini API keys found in configuration (.env)!")
        log_print("  Please configure GEMINI_API_KEYS in .env and retry.")
        log_print(f"{'='*65}\n")
        return False

    log_print(f"  Found {len(raw_keys)} configured Gemini API key(s):")
    for idx, key in enumerate(raw_keys, start=1):
        log_print(f"  - Key #{idx}: {redact_key(key)}")

    # Step 2: Individual Health-Check (Ping Test)
    log_print(f"\n[2/5] INDIVIDUAL HEALTH-CHECKS (PING TEST):")
    all_healthy = True
    active_models_used = set()

    for idx, key in enumerate(raw_keys, start=1):
        res = ping_gemini_key(key, model_name=settings.GEMINI_MODEL)
        status_tag = f"[{res['status']}]"
        active_models_used.add(res["model"])
        log_print(f"  - Key #{idx} ({redact_key(key)}): {status_tag:<8} {res['message']}")
        if res["status"] == "FAIL":
            all_healthy = False

    # Step 3: Rotation & Failover Test
    log_print(f"\n[3/5] ROTATION & FAILOVER VALIDATION:")
    km = GeminiKeyManager(keys=raw_keys, rpm_per_key=100)
    acquired_keys: List[str] = []

    for req_idx in range(len(raw_keys)):
        k = km.acquire_key()
        acquired_keys.append(k)
        log_print(f"  - Request {req_idx + 1}: Acquired {redact_key(k)}")
        km.release_key(k, success=True)

    # Verify uniqueness in a single cycle
    if len(set(acquired_keys)) == len(raw_keys):
        log_print(f"  [PASS] Successfully rotated across all {len(raw_keys)} keys in round-robin order.")
    else:
        log_print("  [WARN] Rotation sequence did not span all unique keys.")

    # Step 4: Live Smoke Test on Contractor Data
    log_print(f"\n[4/5] LIVE CONTRACTOR SMOKE ANALYSIS (KAFUE EARTHMOVING LTD):")
    contractor_data = {
        "company_name": "Kafue Earthmoving Ltd",
        "location_district": "Plot 492 Industrial Area, Kafue, Zambia",
        "industry_sector": "Civil Earthmoving & Equipment Hire",
        "phone_normalized": "+260977112233",
        "fb_page_url": "https://facebook.com/kafueearthmoving",
        "description_raw": (
            "Earthmoving equipment hire, CAT 320D excavators, 20-ton tipper trucks, "
            "road grading and quarry aggregate transport in Kafue & Lusaka corridor."
        ),
    }

    smoke_km = GeminiKeyManager(keys=raw_keys, rpm_per_key=100)
    client = GeminiClient(key_manager=smoke_km, model_name=settings.GEMINI_MODEL)

    try:
        analysis_result = client.analyze_business(contractor_data)
        log_print("  [PASS] Live Gemini Analysis Succeeded! Structured output:")
        log_print(f"  • Sector Match:     {analysis_result.get('sector_match')}")
        log_print(f"  • Primary Sector:   {analysis_result.get('primary_sector')}")
        log_print(f"  • Asset Footprint:  {analysis_result.get('asset_footprint')}")
        log_print(f"  • Digital Deficits: {analysis_result.get('digital_deficits')}")
        log_print(f"  • Confidence Score: {analysis_result.get('confidence_score')}")
        log_print(f"  • Evidence Summary: {analysis_result.get('evidence_summary')}")
    except Exception as e:
        log_print(f"  [FAIL] Live smoke analysis failed: {e}")
        all_healthy = False

    # Step 5: Security Assertion (Zero Raw Key Exposure)
    log_print(f"\n[5/5] SECURITY ASSERTION (CREDENTIAL REDACTION):")
    full_output = "\n".join(output_lines)
    leaked = False
    for k in raw_keys:
        if k in full_output:
            leaked = True
            break

    if leaked:
        log_print("  [CRITICAL FAIL] Unredacted API key detected in output stream!")
        return False
    else:
        log_print("  [PASS] Zero raw API keys exposed. All credentials strictly redacted.")

    log_print(f"\n{'='*65}")
    if all_healthy:
        log_print("  LIVE GEMINI VERIFICATION STATUS: 100% OPERATIONAL & READY")
    else:
        log_print("  LIVE GEMINI VERIFICATION STATUS: WARNINGS / FAILURES DETECTED")
    log_print(f"{'='*65}\n")

    return all_healthy
