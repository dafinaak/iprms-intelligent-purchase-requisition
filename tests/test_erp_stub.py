import json
from datetime import datetime, timezone

import erp_stub
from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from agents import agent_c_budget_validation as agent_c
from agents import agent_d_vendor_matching as agent_d
from agents import agent_e_policy_compliance as agent_e
from agents import agent_f_sole_source_bid_threshold as agent_f
from agents import agent_g_split_order_anomaly as agent_g
from agents import agent_h_exception_triage_orchestration as agent_h
from configs.config import PR_BUNDLES_DIR

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _run_erp(bundle_name, tmp_path):
    bundle = PR_BUNDLES_DIR / bundle_name
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    for m in (agent_b, agent_c, agent_d, agent_e, agent_f, agent_g, agent_h):
        m.run(ares)
    return ares, erp_stub.run(ares)


def test_clean_pr_posts_successfully(tmp_path):
    _ares, res = _run_erp("pr_bundle_001", tmp_path)
    erp = res.erp_posting_result
    assert erp.po_status == "ready_for_posting"
    assert erp.erp_status == "simulated_post_success"
    assert erp.posted is True
    assert erp.po_number
    # clean PR -> no tracker payload created
    assert res.tracker_payload is None
    assert res.tracker_payload_path is None


def test_budget_blocked_not_posted_and_tracker_to_fpa(tmp_path):
    _ares, res = _run_erp("scenario_04_budget_exhausted", tmp_path)
    erp = res.erp_posting_result
    assert erp.erp_status == "not_posted"
    assert erp.posted is False
    assert res.tracker_payload is not None
    assert res.tracker_payload.routed_to == "FP&A"


def test_non_preferred_tracker_to_procurement(tmp_path):
    _ares, res = _run_erp("scenario_02_non_preferred_vendor", tmp_path)
    assert res.erp_posting_result.erp_status == "not_posted"
    assert res.tracker_payload.routed_to == "Procurement"


def test_split_order_tracker_to_compliance(tmp_path):
    _ares, res = _run_erp("scenario_07_split_order_pattern", tmp_path)
    assert res.tracker_payload.routed_to == "Compliance"


def test_low_confidence_tracker_to_manual_review(tmp_path):
    _ares, res = _run_erp("scenario_08_low_confidence_extraction", tmp_path)
    assert res.tracker_payload.routed_to == "Manual Review"


def test_artifacts_written(tmp_path):
    ares, res = _run_erp("scenario_04_budget_exhausted", tmp_path)
    assert (ares.run_dir / "erp_posting_result.json").exists()
    assert (ares.run_dir / "tracker_payload.json").exists()
    data = json.loads(res.erp_posting_result_path.read_text(encoding="utf-8"))
    assert data["erp_status"] == "not_posted"
