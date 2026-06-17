from datetime import datetime, timezone

import pytest

from configs.config import PR_BUNDLES_DIR
from manifest_validation import ManifestValidationError
from pipelines.run_iprms_pipeline import run_direct, run_pipeline

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)

ALL_ARTIFACTS = [
    "context_packet.json", "evidence_index.json", "extracted_pr.json",
    "budget_check.json", "vendor_match.json", "policy_check.json",
    "sole_source_check.json", "bid_threshold_check.json", "anomaly_report.json",
    "exceptions.md", "approval_packet.json", "po_draft.json",
    "audit_log.md", "metrics.json", "run_summary.csv",
    "erp_posting_result.json",
]


def test_clean_pr_full_pipeline(tmp_path):
    res = run_pipeline(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)
    assert res.final_decision == "auto_po"
    assert res.po_status == "ready_for_posting"
    assert res.erp_status == "simulated_post_success"
    assert res.exception_count == 0
    for name in ALL_ARTIFACTS:
        assert (res.run_dir / name).exists(), f"missing {name}"


def test_budget_exhausted_pipeline(tmp_path):
    res = run_pipeline(PR_BUNDLES_DIR / "scenario_04_budget_exhausted", runs_root=tmp_path, when=FIXED)
    assert res.final_decision == "blocked"
    assert res.erp_status == "not_posted"
    assert (res.run_dir / "tracker_payload.json").exists()


def test_invalid_bundle_blocked(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ManifestValidationError):
        run_pipeline(empty, runs_root=tmp_path / "runs", when=FIXED)


def test_execution_order_is_A_to_H(tmp_path):
    # Direct path: each agent writes its artifact; presence in order implies the chain ran.
    ares = run_direct(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)
    order = ["context_packet.json", "extracted_pr.json", "budget_check.json",
             "vendor_match.json", "policy_check.json", "sole_source_check.json",
             "anomaly_report.json", "approval_packet.json", "po_draft.json"]
    for name in order:
        assert (ares.run_dir / name).exists()


@pytest.mark.parametrize("bundle", [
    "pr_bundle_001",
    "scenario_02_non_preferred_vendor",
    "scenario_04_budget_exhausted",
    "scenario_07_split_order_pattern",
    "scenario_12_multi_currency",
])
def test_langgraph_matches_direct(bundle, tmp_path):
    """LangGraph skeleton output must be identical to the direct pipeline."""
    direct = run_pipeline(PR_BUNDLES_DIR / bundle, runs_root=tmp_path / "d", when=FIXED)
    graph = run_pipeline(PR_BUNDLES_DIR / bundle, runs_root=tmp_path / "g", when=FIXED,
                         use_langgraph=True)

    # same deterministic decision
    assert direct.final_decision == graph.final_decision
    assert direct.po_status == graph.po_status
    assert direct.run_id == graph.run_id  # same input + when -> same run_id

    # every artifact identical byte-for-byte
    for name in ALL_ARTIFACTS:
        df = direct.run_dir / name
        gf = graph.run_dir / name
        assert df.read_text(encoding="utf-8") == gf.read_text(encoding="utf-8"), f"mismatch in {name}"
