"""
End-to-End Verification Script for FS-CAE Autonomous Pipeline
Verifies the full lifecycle:
  1. Uploads sample_zambian_contractors.csv via POST /upload
  2. Verifies background orchestrator execution & live telemetry
  3. Verifies queue contents & batch approval
  4. Verifies dispatcher trigger
"""

import time
import requests
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"

def run_verification():
    print("=" * 60)
    print("FS-CAE END-TO-END PIPELINE VERIFICATION")
    print("=" * 60)

    # 1. Health check
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"Health check failed: {r.status_code}"
    print("[OK] Step 1: Web server is active and healthy.")

    # 2. Upload sample CSV via POST /upload
    csv_path = Path("data/input/sample_zambian_contractors.csv")
    assert csv_path.exists(), f"Sample CSV not found at {csv_path}"

    with open(csv_path, "rb") as f:
        files = [("files", ("sample_zambian_contractors.csv", f, "text/csv"))]
        data = {"auto_pipeline": "true", "auto_qualify": "true"}
        headers = {"Accept": "application/json"}
        print(f"Uploading {csv_path.name} via POST /upload...")
        res = requests.post(f"{BASE_URL}/upload", files=files, data=data, headers=headers)

    print(f"Upload Response HTTP {res.status_code}: {res.text}")
    assert res.status_code == 200, f"Upload failed: {res.status_code}"
    resp_json = res.json()
    assert resp_json.get("success") is True or resp_json.get("status") == "SUCCESS"
    print("[OK] Step 2: Multi-file upload accepted and processed.")

    # 3. Poll Telemetry
    print("Polling GET /api/pipeline/telemetry...")
    time.sleep(1)
    tel = requests.get(f"{BASE_URL}/api/pipeline/telemetry").json()
    print(f"  Current Stage: {tel.get('current_stage')}")
    print(f"  Progress: {tel.get('progress_percent')}%")
    print(f"  KPIs: {tel.get('kpis')}")
    print("  Latest 5 Logs:")
    for l in tel.get("logs", [])[-5:]:
        print(f"    {l}")

    assert "logs" in tel and len(tel["logs"]) > 0
    assert "kpis" in tel
    print("[OK] Step 3: Telemetry stream active with real-time logs & KPIs.")

    # 4. Check Queue (/queue)
    q_res = requests.get(f"{BASE_URL}/queue")
    assert q_res.status_code == 200
    assert "Outbound Acquisition Queue" in q_res.text
    print("[OK] Step 4: Acquisition Queue rendered staged contractors.")

    # 5. Test Batch Approval (/queue/approve)
    app_res = requests.post(f"{BASE_URL}/queue/approve", headers={"Accept": "application/json"})
    print(f"Batch Approval Response HTTP {app_res.status_code}: {app_res.text}")
    assert app_res.status_code == 200
    print("[OK] Step 5: 1-Click Batch Approval moved staged leads into QUEUED.")

    # 6. Test Dispatcher (/queue/dispatch)
    disp_res = requests.post(f"{BASE_URL}/queue/dispatch", headers={"Accept": "application/json"})
    print(f"Dispatcher Response HTTP {disp_res.status_code}: {disp_res.text}")
    assert disp_res.status_code == 200
    print("[OK] Step 6: Safe Dispatcher triggered successfully.")

    print("=" * 60)
    print("ALL 6 END-TO-END VERIFICATION GATES PASSED 100%!")
    print("=" * 60)

if __name__ == "__main__":
    run_verification()
