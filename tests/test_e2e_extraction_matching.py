import csv
from datetime import datetime, timezone

import pytest

from configs.config import PR_BUNDLES_DIR
from pipelines.run_iprms_pipeline import run_direct
from schemas.pr_schema import BudgetCheck, BudgetResult, ExtractedPR, VendorMatch, VendorMatchResult

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)

BUNDLES = [
    "pr_bundle_001",
    "scenario_01_clean_pr",
    "scenario_02_non_preferred_vendor",
    "scenario_04_budget_exhausted",
    "scenario_06_vague_item_description",
    "scenario_08_low_confidence_extraction",
    "scenario_12_multi_currency",
]


@pytest.mark.parametrize("bundle", BUNDLES)
def test_extraction_matching_artifacts_valid(bundle, tmp_path):
    ares = run_direct(PR_BUNDLES_DIR / bundle, runs_root=tmp_path, when=FIXED)
    run_dir = ares.run_dir

    # outputs stored under runs/<run_id>/
    assert run_dir.parent == tmp_path
    for name in ("extracted_pr.json", "budget_check.json", "vendor_match.json"):
        assert (run_dir / name).exists(), f"{bundle}: missing {name}"

    # schema-valid
    extracted = ExtractedPR.model_validate_json((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))
    budget = BudgetCheck.model_validate_json((run_dir / "budget_check.json").read_text(encoding="utf-8"))
    vendor = VendorMatch.model_validate_json((run_dir / "vendor_match.json").read_text(encoding="utf-8"))

    # extraction: per-field provenance + overall score
    assert extracted.pr_id.value
    assert 0.0 <= extracted.confidence_score <= 1.0
    assert isinstance(budget.result, BudgetResult)
    assert isinstance(vendor.result, VendorMatchResult)

    # consistency across agents (same PR id / cost center / vendor)
    assert budget.pr_id == extracted.pr_id.value
    assert budget.cost_center == extracted.cost_center.value
    assert vendor.vendor_name == extracted.vendor_name.value


def test_evidence_references_resolve(tmp_path):
    ares = run_direct(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)
    # evidence_index maps the supporting roles to existing files on disk
    roles = {e["role"]: e for e in ares.evidence_index["evidence"]}
    for role in ("budget_snapshot", "approved_vendors", "catalogue_pricing",
                 "cost_center_mapping", "requisition"):
        assert role in roles
        from pathlib import Path
        assert Path(roles[role]["path"]).exists()
        assert roles[role]["sha256"]  # evidence is hashed


def test_bounding_box_present_for_pdf(tmp_path):
    ares = run_direct(PR_BUNDLES_DIR / "pr_bundle_001", runs_root=tmp_path, when=FIXED)
    extracted = ExtractedPR.model_validate_json((ares.run_dir / "extracted_pr.json").read_text(encoding="utf-8"))
    # at least one field located on the PDF page
    assert extracted.vendor_name.bounding_box is not None
    assert extracted.vendor_name.source_page == 1


def _summary_row(run_dir):
    with (run_dir / "run_summary.csv").open(encoding="utf-8") as f:
        return next(csv.DictReader(f))


def test_run_summary_reflects_extraction_status(tmp_path):
    # low-confidence extraction (scenario_08) -> manual_review in run_summary
    ares = run_direct(PR_BUNDLES_DIR / "scenario_08_low_confidence_extraction", runs_root=tmp_path, when=FIXED)
    row = _summary_row(ares.run_dir)
    assert row["final_decision"] == "manual_review"
    assert row["pr_id"]


def test_run_summary_reflects_matching_status(tmp_path):
    # non-preferred vendor (scenario_02) -> exception routed to Procurement
    ares = run_direct(PR_BUNDLES_DIR / "scenario_02_non_preferred_vendor", runs_root=tmp_path, when=FIXED)
    row = _summary_row(ares.run_dir)
    assert row["final_decision"] == "exception"
    assert row["routed_to"] == "Procurement"
    assert row["vendor_name"] == "QuickSupply"
