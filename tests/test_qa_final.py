import csv
import json
import shutil
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agents import agent_a_intake_context as agent_a
from agents import agent_e_policy_compliance as agent_e
from agents import agent_h_exception_triage_orchestration as agent_h
from configs.config import PR_BUNDLES_DIR
from pipelines.run_iprms_pipeline import run_direct, run_pipeline
from reporting import validate_run
from schemas.decision_schema import FinalDecision
from schemas.pr_schema import ClassificationSource, PRType

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)

SCENARIOS = [
    "scenario_01_clean_pr", "scenario_02_non_preferred_vendor",
    "scenario_03_same_week_threshold_anomaly", "scenario_04_budget_exhausted",
    "scenario_05_emergency_sole_source", "scenario_06_vague_item_description",
    "scenario_07_split_order_pattern", "scenario_08_low_confidence_extraction",
    "scenario_09_small_clean_pr", "scenario_10_framework_agreement",
    "scenario_11_blanket_order", "scenario_12_multi_currency",
]

MANDATORY = [
    "context_packet.json", "evidence_index.json", "extracted_pr.json",
    "budget_check.json", "vendor_match.json", "policy_check.json",
    "sole_source_check.json", "bid_threshold_check.json", "anomaly_report.json",
    "exceptions.md", "approval_packet.json", "po_draft.json",
    "audit_log.md", "metrics.json", "run_summary.csv", "erp_posting_result.json",
]

VALID_DECISIONS = {d.value for d in FinalDecision}


# ---- all 12 scenarios end-to-end ----
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_e2e_artifacts_and_validation(scenario, tmp_path):
    res = run_pipeline(PR_BUNDLES_DIR / scenario, runs_root=tmp_path, when=FIXED)
    run_dir = res.run_dir

    # local run folder under runs root
    assert run_dir.is_dir() and run_dir.parent == tmp_path
    # all mandatory artifacts present
    for name in MANDATORY:
        assert (run_dir / name).exists(), f"{scenario}: missing {name}"
    # every schema-backed artifact validates
    report = validate_run(run_dir)
    assert report.all_valid, [r for r in report.results if not r.valid]
    # run_summary.csv carries a valid decision
    with (run_dir / "run_summary.csv").open(encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["final_decision"] in VALID_DECISIONS
    assert row["pr_id"]


# ---- key scenario decisions (plan §8 semantics) ----
def test_key_scenario_decisions(tmp_path):
    expected = {
        "scenario_01_clean_pr": "auto_po",
        "scenario_02_non_preferred_vendor": "exception",
        "scenario_04_budget_exhausted": "blocked",
        "scenario_05_emergency_sole_source": "expedited_approval",
        "scenario_06_vague_item_description": "buyer_clarification",
        "scenario_07_split_order_pattern": "exception",
        "scenario_08_low_confidence_extraction": "manual_review",
        "scenario_12_multi_currency": "manual_approval",
    }
    for scenario, decision in expected.items():
        res = run_pipeline(PR_BUNDLES_DIR / scenario, runs_root=tmp_path / scenario, when=FIXED)
        assert res.final_decision == decision, f"{scenario}: {res.final_decision} != {decision}"


def test_complex_flags_for_framework_and_blanket(tmp_path):
    for scenario, flag in (("scenario_10_framework_agreement", "framework_agreement_flag"),
                           ("scenario_11_blanket_order", "blanket_order_flag")):
        res = run_pipeline(PR_BUNDLES_DIR / scenario, runs_root=tmp_path / scenario, when=FIXED)
        pol = json.loads((res.run_dir / "policy_check.json").read_text(encoding="utf-8"))
        assert pol[flag] is True


# ---- idempotent re-run (plan §13) ----
def test_idempotent_rerun_same_decision_and_hash(tmp_path):
    a = run_pipeline(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path / "a", when=FIXED)
    b = run_pipeline(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path / "b", when=FIXED)
    assert a.run_id == b.run_id
    assert a.final_decision == b.final_decision
    # identical decision + metrics artifacts
    for name in ("approval_packet.json", "metrics.json", "po_draft.json"):
        assert (a.run_dir / name).read_text(encoding="utf-8") == (b.run_dir / name).read_text(encoding="utf-8")
    m = json.loads((a.run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert m["idempotency_check"] == "passed" and len(m["input_hash"]) == 64


# ---- LangGraph parity ----
def test_langgraph_matches_direct(tmp_path):
    direct = run_pipeline(PR_BUNDLES_DIR / "scenario_07_split_order_pattern",
                          runs_root=tmp_path / "d", when=FIXED)
    graph = run_pipeline(PR_BUNDLES_DIR / "scenario_07_split_order_pattern",
                         runs_root=tmp_path / "g", when=FIXED, use_langgraph=True)
    assert direct.final_decision == graph.final_decision
    for name in MANDATORY:
        assert (direct.run_dir / name).read_text(encoding="utf-8") == \
               (graph.run_dir / name).read_text(encoding="utf-8"), f"mismatch {name}"


# ---- Agent A PR-type fallback: disabled (deterministic) and mocked ----
def test_agent_a_pr_type_fallback_disabled_and_mocked(tmp_path):
    dst = tmp_path / "bundle"
    shutil.copytree(PR_BUNDLES_DIR / "pr_bundle_001", dst)
    (dst / "requisition_form.json").unlink()  # metadata insufficient

    # disabled -> deterministic DEFAULT, no trace
    res = agent_a.run(dst, runs_root=tmp_path / "r1", when=FIXED)
    assert res.classification.source == ClassificationSource.DEFAULT
    assert res.llm_fallback_used is False
    assert not (res.run_dir / "llm_fallback_trace.json").exists()

    # mocked -> LLM suggestion + trace written
    res2 = agent_a.run(dst, runs_root=tmp_path / "r2", when=FIXED,
                       pr_type_classifier=lambda meta: "capex")
    assert res2.classification.source == ClassificationSource.LLM
    assert res2.classification.pr_type == PRType.CAPEX
    assert (res2.run_dir / "llm_fallback_trace.json").exists()


def test_llm_does_not_change_final_decision(tmp_path):
    # pr_type is advisory: overriding it must not change Agent H's rule-based decision.
    ares = run_direct(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)
    baseline = json.loads((ares.run_dir / "approval_packet.json").read_text(encoding="utf-8"))["final_decision"]

    cp = ares.run_dir / "context_packet.json"
    ctx = json.loads(cp.read_text(encoding="utf-8"))
    ctx["pr_type"] = "capex"  # tamper the (advisory) PR type
    cp.write_text(json.dumps(ctx), encoding="utf-8")
    res = agent_h.run(ares)
    assert res.approval_packet.final_decision.value == baseline  # unchanged


# ---- Agent B fallback determinism (LLM off) ----
def test_agent_b_deterministic_with_llm_off(tmp_path):
    a = run_direct(PR_BUNDLES_DIR / "scenario_06_vague_item_description", runs_root=tmp_path / "a", when=FIXED)
    b = run_direct(PR_BUNDLES_DIR / "scenario_06_vague_item_description", runs_root=tmp_path / "b", when=FIXED)
    assert (a.run_dir / "extracted_pr.json").read_text(encoding="utf-8") == \
           (b.run_dir / "extracted_pr.json").read_text(encoding="utf-8")


# ---- policy change behaviour ----
def test_policy_change_changes_outcome(tmp_path):
    ares = run_direct(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)  # amount 450
    default_pol = {"approval_thresholds": {"manager_limit": 1000, "finance_limit": 5000, "director_limit": 10000}}
    strict_pol = {"approval_thresholds": {"manager_limit": 100, "finance_limit": 5000, "director_limit": 10000}}

    base = agent_e.run(ares, policy=default_pol).policy_check
    strict = agent_e.run(ares, policy=strict_pol).policy_check
    assert base.manual_approval_required is False and base.approval_level_required == "Manager"
    assert strict.manual_approval_required is True   # 450 > 100 now requires higher approval
    assert strict.approval_level_required == "Finance"


# ---- demo UI / API readiness ----
def test_api_demo_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "RUNS_DIR", tmp_path)
    client = TestClient(api_main.app)
    r = client.post("/run-pr", json={"bundle_dir": str(PR_BUNDLES_DIR / "pr_bundle_001")})
    assert r.status_code == 200 and r.json()["final_decision"] == "auto_po"
    assert client.get("/metrics").json()["total_runs"] == 1
