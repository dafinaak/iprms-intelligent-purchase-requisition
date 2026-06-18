import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from configs.config import PR_BUNDLES_DIR

BUNDLE = str(PR_BUNDLES_DIR / "pr_bundle_001")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Redirect runs to a temp dir so tests don't pollute runs/.
    monkeypatch.setattr(api_main, "RUNS_DIR", tmp_path)
    return TestClient(api_main.app)


def test_run_pr_and_read_decision(client):
    r = client.post("/run-pr", json={"bundle_dir": BUNDLE})
    assert r.status_code == 200
    body = r.json()
    assert body["final_decision"] == "auto_po"
    assert body["po_status"] == "ready_for_posting"
    run_id = body["run_id"]

    d = client.get(f"/runs/{run_id}/decision")
    assert d.status_code == 200
    assert d.json()["final_decision"] == "auto_po"


def test_get_run_and_artifacts(client):
    run_id = client.post("/run-pr", json={"bundle_dir": BUNDLE}).json()["run_id"]

    run = client.get(f"/runs/{run_id}").json()
    assert run["bundle_id"] == "pr_bundle_001"
    assert run["pr_type"] == "standard"

    arts = client.get(f"/runs/{run_id}/artifacts").json()["artifacts"]
    for name in ("context_packet.json", "po_draft.json", "run_summary.csv",
                 "erp_posting_result.json"):
        assert name in arts


def test_summary_and_metrics(client):
    client.post("/run-pr", json={"bundle_dir": str(PR_BUNDLES_DIR / "scenario_04_budget_exhausted")})
    run_id = client.post("/run-pr", json={"bundle_dir": BUNDLE}).json()["run_id"]

    summary = client.get(f"/runs/{run_id}/summary").json()
    assert summary["run_summary"]["final_decision"] == "auto_po"

    metrics = client.get("/metrics").json()
    assert metrics["total_runs"] == 2
    assert metrics["by_decision"].get("auto_po") == 1
    assert metrics["by_decision"].get("blocked") == 1


def test_audit_log_endpoint(client):
    run_id = client.post("/run-pr", json={"bundle_dir": BUNDLE}).json()["run_id"]
    r = client.get(f"/runs/{run_id}/audit-log")
    assert r.status_code == 200
    assert "Audit Log" in r.text
    assert "auto_po" in r.text


def test_invalid_bundle_returns_400(client, tmp_path):
    empty = tmp_path / "empty_bundle"
    empty.mkdir()
    r = client.post("/run-pr", json={"bundle_dir": str(empty)})
    assert r.status_code == 400


def test_unknown_run_returns_404(client):
    assert client.get("/runs/NOPE-123").status_code == 404
