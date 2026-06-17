import json
from datetime import datetime, timezone

from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from configs.config import PR_BUNDLES_DIR

FIXED = datetime(2026, 6, 16, 14, 30, 22, tzinfo=timezone.utc)


def _run_b(bundle_name, tmp_path, **kwargs):
    bundle = PR_BUNDLES_DIR / bundle_name
    ares = agent_a.run(bundle, runs_root=tmp_path, when=FIXED)
    return ares, agent_b.run(ares, **kwargs)


def test_extracts_values_from_json_and_writes_artifact(tmp_path):
    ares, res = _run_b("pr_bundle_001", tmp_path)
    # artifact written into the same run dir
    assert res.extracted_path == ares.run_dir / "extracted_pr.json"
    assert res.extracted_path.exists()

    pr = res.extracted_pr
    assert pr.pr_id.value == "PR-2026-001"
    assert pr.vendor_name.value == "Gjirafa Mall"
    assert pr.quantity.value == 15 and isinstance(pr.quantity.value, int)
    assert pr.unit_price.value == 30.0 and isinstance(pr.unit_price.value, float)
    assert pr.currency.value == "EUR"
    assert pr.confidence_score == 0.96


def test_bounding_boxes_from_pdf(tmp_path):
    _ares, res = _run_b("pr_bundle_001", tmp_path)
    # vendor name "Gjirafa Mall" exists as words in the PDF -> bbox located
    vn = res.extracted_pr.vendor_name
    assert vn.source_page == 1
    assert vn.bounding_box is not None and len(vn.bounding_box) == 4


def test_low_confidence_finding(tmp_path):
    _ares, res = _run_b("scenario_08_low_confidence_extraction", tmp_path)
    types = {f.finding_type for f in res.findings}
    assert "LOW_CONFIDENCE_EXTRACTION" in types
    assert res.extracted_pr.confidence_score == 0.6


def test_vague_item_finding(tmp_path):
    _ares, res = _run_b("scenario_06_vague_item_description", tmp_path)
    types = {f.finding_type for f in res.findings}
    assert "VAGUE_ITEM_DESCRIPTION" in types          # "IT equipment"
    assert "LOW_CONFIDENCE_EXTRACTION" not in types    # confidence 0.91 >= 0.85


def test_clean_pr_has_no_findings(tmp_path):
    _ares, res = _run_b("pr_bundle_001", tmp_path)
    assert res.findings == []


def test_llm_fallback_off_by_default_and_deterministic(tmp_path):
    _a1, r1 = _run_b("pr_bundle_001", tmp_path / "a")
    _a2, r2 = _run_b("pr_bundle_001", tmp_path / "b")
    assert r1.llm_fallback_used is False
    assert r1.extracted_pr.model_dump(mode="json") == r2.extracted_pr.model_dump(mode="json")


def test_extracted_pr_json_is_valid_schema(tmp_path):
    _ares, res = _run_b("pr_bundle_001", tmp_path)
    data = json.loads(res.extracted_path.read_text(encoding="utf-8"))
    # per-field provenance shape + overall score present
    assert data["vendor_name"]["value"] == "Gjirafa Mall"
    assert "confidence_score" in data


def test_llm_fields_off_by_default(tmp_path):
    _ares, res = _run_b("pr_bundle_001", tmp_path)
    assert res.llm_fallback_used is False
    assert res.llm_normalized_candidate is None
    data = json.loads(res.extracted_path.read_text(encoding="utf-8"))
    assert data["llm_fallback_used"] is False
    assert data["llm_normalized_candidate"] is None
    assert not (res.extracted_path.parent / "llm_fallback_trace.json").exists()


def test_llm_normalizes_vague_without_overwriting_original(tmp_path):
    # Inject a mock LLM; vague "IT equipment" gets a normalized candidate.
    _ares, res = _run_b("scenario_06_vague_item_description", tmp_path,
                        llm_fallback=lambda text: "Dell business laptop")
    # Original parser/OCR value stays authoritative (NOT overwritten).
    assert res.extracted_pr.item_description.value == "IT equipment"
    # Normalized candidate stored separately + flag set.
    assert res.llm_fallback_used is True
    assert res.extracted_pr.llm_normalized_candidate == "Dell business laptop"
    # Audit trace written.
    trace_path = res.extracted_path.parent / "llm_fallback_trace.json"
    assert trace_path.exists()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["fallback_type"] == "item_extraction" and trace["used"] is True
    # Vague finding still raised deterministically.
    assert any(f.finding_type == "VAGUE_ITEM_DESCRIPTION" for f in res.findings)