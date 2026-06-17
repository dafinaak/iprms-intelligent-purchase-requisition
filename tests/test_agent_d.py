import json
from datetime import datetime, timezone

from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from agents import agent_d_vendor_matching as agent_d
from configs.config import PR_BUNDLES_DIR
from schemas.pr_schema import VendorMatchResult

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _run_d(bundle_name, tmp_path):
    bundle = PR_BUNDLES_DIR / bundle_name
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    return ares, agent_d.run(ares)


def test_preferred_vendor_matched(tmp_path):
    _ares, res = _run_d("pr_bundle_001", tmp_path)  # Gjirafa Mall = approved+preferred
    vm = res.vendor_match
    assert vm.result == VendorMatchResult.MATCHED
    assert vm.approved is True and vm.preferred is True
    assert res.match_score == 1.0
    assert res.findings == []


def test_non_preferred_without_justification_is_exception(tmp_path):
    _ares, res = _run_d("scenario_02_non_preferred_vendor", tmp_path)  # QuickSupply, no justification
    vm = res.vendor_match
    assert vm.result == VendorMatchResult.NON_PREFERRED
    assert vm.approved is True and vm.preferred is False
    assert vm.justification_present is False
    f = next(f for f in res.findings if f.finding_type == "NON_PREFERRED_VENDOR")
    assert f.severity.value == "high"
    assert "Procurement" in f.recommended_action


def test_non_preferred_with_justification_no_exception(tmp_path):
    # Same bundle but add a written justification -> downgraded finding.
    bundle = PR_BUNDLES_DIR / "scenario_02_non_preferred_vendor"
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    ep = ares.run_dir / "extracted_pr.json"
    data = json.loads(ep.read_text(encoding="utf-8"))
    data["business_justification"]["value"] = "Sole local supplier with fastest delivery."
    ep.write_text(json.dumps(data), encoding="utf-8")

    res = agent_d.run(ares)
    assert res.vendor_match.result == VendorMatchResult.NON_PREFERRED
    assert res.vendor_match.justification_present is True
    f = next(f for f in res.findings if f.finding_type == "NON_PREFERRED_VENDOR")
    assert f.severity.value == "low"


def test_unknown_vendor_not_approved(tmp_path):
    bundle = PR_BUNDLES_DIR / "pr_bundle_001"
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    agent_b.run(ares)
    ep = ares.run_dir / "extracted_pr.json"
    data = json.loads(ep.read_text(encoding="utf-8"))
    data["vendor_name"]["value"] = "Totally Unknown Vendor LLC"
    ep.write_text(json.dumps(data), encoding="utf-8")

    res = agent_d.run(ares)
    assert res.vendor_match.result == VendorMatchResult.NOT_APPROVED
    assert res.vendor_match.approved is False
    assert any(f.finding_type == "VENDOR_NOT_APPROVED" for f in res.findings)


def test_vendor_match_json_written_and_serialized(tmp_path):
    _ares, res = _run_d("pr_bundle_001", tmp_path)
    assert res.vendor_match_path.name == "vendor_match.json"
    data = json.loads(res.vendor_match_path.read_text(encoding="utf-8"))
    assert data["result"] == "matched"
    assert isinstance(data["result"], str)
    assert data["vendor_name"] == "Gjirafa Mall"
