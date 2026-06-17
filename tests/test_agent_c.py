import json
from datetime import datetime, timezone

from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from agents import agent_c_budget_validation as agent_c
from configs.config import PR_BUNDLES_DIR
from schemas.pr_schema import BudgetResult

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _run_c(bundle_name, tmp_path):
    bundle = PR_BUNDLES_DIR / bundle_name
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    return ares, agent_c.run(ares)


def test_budget_passed_clean(tmp_path):
    _ares, res = _run_c("pr_bundle_001", tmp_path)
    bc = res.budget_check
    assert bc.result == BudgetResult.PASSED
    assert bc.cost_center_exists is True
    assert bc.available_budget >= bc.requested_amount
    assert bc.routed_to is None
    assert res.findings == []


def test_budget_exhausted_routes_to_fpa(tmp_path):
    _ares, res = _run_c("scenario_04_budget_exhausted", tmp_path)
    bc = res.budget_check
    assert bc.result == BudgetResult.FAILED_BUDGET_EXHAUSTED
    assert bc.available_budget < bc.requested_amount
    assert bc.routed_to == "FP&A"
    assert any(f.finding_type == "BUDGET_EXCEEDED" for f in res.findings)


def test_cost_center_not_found_routes_to_fpa(tmp_path):
    # Run A+B, then tamper extracted_pr.json with an unknown cost center.
    bundle = PR_BUNDLES_DIR / "pr_bundle_001"
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    ep = ares.run_dir / "extracted_pr.json"
    data = json.loads(ep.read_text(encoding="utf-8"))
    data["cost_center"]["value"] = "CC-DOES-NOT-EXIST"
    ep.write_text(json.dumps(data), encoding="utf-8")

    res = agent_c.run(ares)
    assert res.budget_check.cost_center_exists is False
    assert res.budget_check.result == BudgetResult.ROUTE_TO_FPA
    assert res.budget_check.routed_to == "FP&A"
    assert any(f.finding_type == "COST_CENTER_NOT_FOUND" for f in res.findings)


def test_budget_check_json_written_and_serialized(tmp_path):
    _ares, res = _run_c("pr_bundle_001", tmp_path)
    assert res.budget_check_path.name == "budget_check.json"
    data = json.loads(res.budget_check_path.read_text(encoding="utf-8"))
    assert data["result"] == "passed"           # enum serialized as string
    assert isinstance(data["result"], str)
    assert data["cost_center"] == "CC-IT-001"
    assert data["findings"] == []
