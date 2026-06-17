import json
from datetime import datetime, timezone

from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from agents import agent_c_budget_validation as agent_c
from agents import agent_d_vendor_matching as agent_d
from agents import agent_e_policy_compliance as agent_e
from configs.config import PR_BUNDLES_DIR

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _run_e(bundle_name, tmp_path):
    bundle = PR_BUNDLES_DIR / bundle_name
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    agent_c.run(ares)
    agent_d.run(ares)
    return ares, agent_e.run(ares)


def test_clean_pr_compliant_auto(tmp_path):
    _ares, res = _run_e("pr_bundle_001", tmp_path)
    pc = res.policy_check
    assert pc.compliance_status == "compliant"
    assert pc.approval_level_required == "Manager"   # 450 <= 1000
    assert pc.manual_approval_required is False
    assert pc.violations == []
    assert res.findings == []


def test_non_preferred_without_justification_is_violation(tmp_path):
    _ares, res = _run_e("scenario_02_non_preferred_vendor", tmp_path)
    pc = res.policy_check
    assert pc.compliance_status == "violation"
    assert "non_preferred_vendor_without_justification" in pc.violations
    assert pc.manual_approval_required is True
    assert any(f.finding_type == "POLICY_VIOLATION_NON_PREFERRED_VENDOR" for f in res.findings)


def test_budget_exhausted_is_violation(tmp_path):
    _ares, res = _run_e("scenario_04_budget_exhausted", tmp_path)
    pc = res.policy_check
    assert pc.budget_ok is False
    assert "budget" in pc.violations
    assert pc.compliance_status == "violation"
    assert any(f.finding_type == "POLICY_VIOLATION_BUDGET" for f in res.findings)


def test_approval_level_scales_with_amount(tmp_path):
    # Tamper amount above finance limit -> Director, manual approval required.
    bundle = PR_BUNDLES_DIR / "pr_bundle_001"
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    agent_c.run(ares)
    agent_d.run(ares)
    ep = ares.run_dir / "extracted_pr.json"
    data = json.loads(ep.read_text(encoding="utf-8"))
    data["estimated_amount"]["value"] = 8000.0
    ep.write_text(json.dumps(data), encoding="utf-8")

    res = agent_e.run(ares)
    pc = res.policy_check
    assert pc.approval_level_required == "Director"   # 5000 < 8000 <= 10000
    assert pc.manual_approval_required is True


def test_policy_check_json_written_with_flags(tmp_path):
    _ares, res = _run_e("pr_bundle_001", tmp_path)
    data = json.loads(res.policy_check_path.read_text(encoding="utf-8"))
    # clean PR: all complex-procurement flags False
    for flag in ("framework_agreement_flag", "blanket_order_flag",
                 "emergency_procurement_flag", "multi_currency_flag"):
        assert data[flag] is False
    assert data["compliance_status"] == "compliant"


# ---------- Task 24: complex procurement flags ----------
def test_framework_agreement_flag(tmp_path):
    _ares, res = _run_e("scenario_10_framework_agreement", tmp_path)
    assert res.policy_check.framework_agreement_flag is True
    assert any(f.finding_type == "FRAMEWORK_AGREEMENT" for f in res.findings)


def test_blanket_order_flag(tmp_path):
    _ares, res = _run_e("scenario_11_blanket_order", tmp_path)
    assert res.policy_check.blanket_order_flag is True
    assert any(f.finding_type == "BLANKET_ORDER" for f in res.findings)


def test_multi_currency_flag_requires_approval(tmp_path):
    _ares, res = _run_e("scenario_12_multi_currency", tmp_path)  # USD
    pc = res.policy_check
    assert pc.multi_currency_flag is True
    assert pc.manual_approval_required is True
    assert any(f.finding_type == "MULTI_CURRENCY" for f in res.findings)


def test_emergency_procurement_flag(tmp_path):
    _ares, res = _run_e("scenario_05_emergency_sole_source", tmp_path)
    pc = res.policy_check
    assert pc.emergency_procurement_flag is True
    assert pc.manual_approval_required is True
    assert any(f.finding_type == "EMERGENCY_PROCUREMENT" for f in res.findings)


def test_clean_pr_no_complex_flags(tmp_path):
    _ares, res = _run_e("pr_bundle_001", tmp_path)
    pc = res.policy_check
    assert not pc.framework_agreement_flag
    assert not pc.blanket_order_flag
    assert not pc.emergency_procurement_flag
    assert not pc.multi_currency_flag
