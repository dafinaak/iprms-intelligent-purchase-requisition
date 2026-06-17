import json
from datetime import datetime, timezone

import reporting
from configs.config import PR_BUNDLES_DIR
from pipelines.run_iprms_pipeline import run_direct

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _full_run(bundle_name, tmp_path):
    return run_direct(PR_BUNDLES_DIR / bundle_name, runs_root=tmp_path, when=FIXED)


def test_all_artifacts_validate_against_schemas(tmp_path):
    ares = _full_run("pr_bundle_001", tmp_path)
    report = reporting.validate_run(ares.run_dir)
    assert report.all_valid, [r for r in report.results if not r.valid]
    # key schema-backed artifacts validated
    names = {r.artifact for r in report.results if r.valid}
    for n in ("extracted_pr.json", "budget_check.json", "vendor_match.json",
              "policy_check.json", "anomaly_report.json", "po_draft.json",
              "metrics.json", "run_summary.csv"):
        assert n in names


def test_summary_from_local_artifacts(tmp_path):
    ares = _full_run("scenario_04_budget_exhausted", tmp_path)
    summary = reporting.summarize_run(ares.run_dir)
    assert summary["run_summary"]["final_decision"] == "blocked"
    assert summary["run_summary"]["po_status"] == "blocked"
    assert summary["metrics"]["idempotency_check"] == "passed"
    assert len(summary["metrics"]["input_hash"]) == 64


def test_validation_catches_corrupt_artifact(tmp_path):
    ares = _full_run("pr_bundle_001", tmp_path)
    # corrupt budget_check.json (confidence/required field broken)
    bc = ares.run_dir / "budget_check.json"
    data = json.loads(bc.read_text(encoding="utf-8"))
    del data["result"]  # required field
    bc.write_text(json.dumps(data), encoding="utf-8")

    report = reporting.validate_run(ares.run_dir)
    assert report.all_valid is False
    bad = next(r for r in report.results if r.artifact == "budget_check.json")
    assert bad.valid is False and bad.error


def test_missing_artifact_reported(tmp_path):
    ares = _full_run("pr_bundle_001", tmp_path)
    (ares.run_dir / "po_draft.json").unlink()
    report = reporting.validate_run(ares.run_dir)
    bad = next(r for r in report.results if r.artifact == "po_draft.json")
    assert bad.exists is False and bad.valid is False
