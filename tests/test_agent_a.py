import json
import shutil
from datetime import datetime, timezone

import pytest

from agents.agent_a_intake_context import (
    AgentAResult,
    _compute_input_hash,
    _generate_run_id,
    classify_pr_type_metadata,
    run,
)
from configs.config import PR_BUNDLES_DIR
from manifest_validation import ManifestValidationError, validate_bundle
from schemas.finding_schema import Finding
from schemas.pr_schema import ClassificationSource, PRType

BUNDLE = PR_BUNDLES_DIR / "pr_bundle_001"
FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def test_run_creates_mandatory_artifacts(tmp_path):
    res = run(BUNDLE, runs_root=tmp_path, when=FIXED)
    assert isinstance(res, AgentAResult)
    assert res.run_dir.is_dir()
    cp = res.run_dir / "context_packet.json"
    ev = res.run_dir / "evidence_index.json"
    assert cp.exists() and ev.exists()

    cpd = json.loads(cp.read_text(encoding="utf-8"))
    assert cpd["bundle_id"] == "pr_bundle_001"
    assert cpd["input_type"] == "pdf"
    assert cpd["input_hash"] == res.input_hash
    assert cpd["gatekeeper"]["manifest_valid"] is True
    assert isinstance(cpd["initial_risk_flags"], list)


def test_invalid_bundle_blocked_and_no_run_dir(tmp_path):
    empty = tmp_path / "empty_bundle"
    empty.mkdir()
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    with pytest.raises(ManifestValidationError):
        run(empty, runs_root=runs_root, when=FIXED)
    assert list(runs_root.iterdir()) == []  # gatekeeper blocked before any dir created


def test_generate_run_id_format():
    rid = _generate_run_id("abcd1234ef", when=FIXED)
    assert rid == "RUN-20260616T143022Z-abcd1234"


def test_run_id_and_hash_are_deterministic(tmp_path):
    a = run(BUNDLE, runs_root=tmp_path / "a", when=FIXED)
    b = run(BUNDLE, runs_root=tmp_path / "b", when=FIXED)
    assert a.input_hash == b.input_hash          # same inputs -> same hash
    assert len(a.input_hash) == 64               # sha256 hex
    assert a.run_id == b.run_id                  # same hash + same timestamp -> same id
    assert a.run_id == f"RUN-20260616T143022Z-{a.input_hash[:8]}"


def test_evidence_index_indexes_all_files(tmp_path):
    res = run(BUNDLE, runs_root=tmp_path, when=FIXED)
    ev = res.evidence_index
    roles = {e["role"] for e in ev["evidence"]}
    assert "requisition" in roles
    assert {"budget_snapshot", "approved_vendors", "catalogue_pricing",
            "approval_policy", "cost_center_mapping", "historical_prs"} <= roles
    assert ev["evidence_count"] == len(ev["evidence"]) == 7
    for e in ev["evidence"]:
        assert e["exists"] is True
        assert e["size_bytes"] > 0
        assert e["sha256"] and len(e["sha256"]) == 64


def test_initial_risk_filter_pdf_flag(tmp_path):
    res = run(BUNDLE, runs_root=tmp_path, when=FIXED)
    assert all(isinstance(f, Finding) for f in res.findings)
    types = {f.finding_type for f in res.findings}
    assert "PDF_EXTRACTION_REQUIRED" in types          # pr_bundle_001 is a digital PDF
    assert "EMPTY_EVIDENCE_FILE" not in types          # all files non-empty


def test_empty_evidence_file_is_flagged(tmp_path):
    # Copy a valid bundle, then blank one supporting file (exists but 0 bytes).
    dst = tmp_path / "bundle"
    shutil.copytree(BUNDLE, dst)
    (dst / "historical_prs.csv").write_text("", encoding="utf-8")
    res = run(dst, runs_root=tmp_path / "runs", when=FIXED)
    empties = [f for f in res.findings if f.finding_type == "EMPTY_EVIDENCE_FILE"]
    assert empties and "historical_prs.csv" in empties[0].message


def test_compute_input_hash_changes_with_content(tmp_path):
    base = validate_bundle(BUNDLE)
    h1 = _compute_input_hash(base, base.requisition_path.parent / "..nonexistent.yaml")

    dst = tmp_path / "bundle"
    shutil.copytree(BUNDLE, dst)
    (dst / "budget_snapshot.csv").write_text("cost_center,total_budget\nCC-IT-001,999\n",
                                             encoding="utf-8")
    changed = validate_bundle(dst)
    h2 = _compute_input_hash(changed, base.requisition_path.parent / "..nonexistent.yaml")
    assert h1 != h2  # different bundle content -> different hash


# ---------- PR-type classification (Task 4 updated) ----------
POLICY = {"approval_thresholds": {"director_limit": 10000}}


def test_classify_metadata_standard_emergency_capex():
    assert classify_pr_type_metadata({"item_category": "IT Equipment", "estimated_amount": 450},
                                     POLICY)[0] == PRType.STANDARD
    assert classify_pr_type_metadata({"emergency": True}, POLICY)[0] == PRType.EMERGENCY
    assert classify_pr_type_metadata({"urgency": "emergency"}, POLICY)[0] == PRType.EMERGENCY
    assert classify_pr_type_metadata({"item_category": "x", "estimated_amount": 12000},
                                     POLICY)[0] == PRType.CAPEX
    # insufficient metadata -> None
    assert classify_pr_type_metadata({}, POLICY) == (None, 0.0)


def test_run_sets_pr_type_from_metadata(tmp_path):
    res = run(BUNDLE, runs_root=tmp_path, when=FIXED)
    c = res.classification
    assert c.pr_type == PRType.STANDARD            # pr_bundle_001 metadata
    assert c.source == ClassificationSource.METADATA
    assert c.llm_fallback_used is False
    cp = res.context_packet
    assert cp["pr_type"] == "standard"
    assert cp["pr_type_source"] == "metadata"
    assert cp["llm_fallback_used"] is False
    assert cp["llm_fallback_trace"] is None
    assert not (res.run_dir / "llm_fallback_trace.json").exists()  # no fallback -> no trace


def test_emergency_scenario_classified(tmp_path):
    bundle = PR_BUNDLES_DIR / "scenario_05_emergency_sole_source"
    res = run(bundle, runs_root=tmp_path, when=FIXED)
    assert res.classification.pr_type == PRType.EMERGENCY
    assert res.classification.source == ClassificationSource.METADATA


def test_llm_fallback_only_when_metadata_insufficient(tmp_path):
    # Copy bundle and remove the JSON metadata so deterministic rules cannot classify.
    dst = tmp_path / "bundle"
    shutil.copytree(BUNDLE, dst)
    (dst / "requisition_form.json").unlink()  # sibling metadata removed (not in manifest)

    # No fallback available -> DEFAULT, no trace file.
    res = run(dst, runs_root=tmp_path / "r1", when=FIXED)
    assert res.classification.source == ClassificationSource.DEFAULT
    assert res.classification.pr_type == PRType.STANDARD
    assert not (res.run_dir / "llm_fallback_trace.json").exists()

    # Injected classifier (mock LLM) -> LLM source + trace written.
    res2 = run(dst, runs_root=tmp_path / "r2", when=FIXED,
               pr_type_classifier=lambda meta: "emergency")
    assert res2.classification.source == ClassificationSource.LLM
    assert res2.classification.pr_type == PRType.EMERGENCY
    assert res2.classification.llm_fallback_used is True
    assert (res2.run_dir / "llm_fallback_trace.json").exists()
    assert res2.context_packet["llm_fallback_trace"] == "llm_fallback_trace.json"


def test_classification_deterministic_without_llm(tmp_path):
    a = run(BUNDLE, runs_root=tmp_path / "a", when=FIXED)
    b = run(BUNDLE, runs_root=tmp_path / "b", when=FIXED)
    assert a.context_packet["pr_type"] == b.context_packet["pr_type"]
    assert a.llm_fallback_used is False and b.llm_fallback_used is False
