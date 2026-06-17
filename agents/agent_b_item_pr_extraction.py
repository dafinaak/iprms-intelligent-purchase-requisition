"""Agent B - Item / PR Extraction (IPRMS).

Agent B extracts structured PR fields from the requisition (plani.pdf §6) and
writes extracted_pr.json (schema: schemas.pr_schema.ExtractedPR).

Extraction strategy (deterministic-first):
  * VALUES come from the structured JSON — reader.fields for json/web_form input,
    or the sibling requisition_form.json for a PDF bundle. This is reliable and
    layout-independent (no brittle PDF label parsing).
  * BOUNDING BOXES come from the PDF word boxes (best-effort per field; None if a
    value is not located on the page).
  * A label-based PDF parser is used ONLY as a fallback for a PDF-only bundle that
    ships no JSON.

LangChain/LLM is an OPTIONAL fallback (off by default) used only when a field is
genuinely unclear (empty); it never overrides clear values and never makes
procurement decisions. The pipeline is fully deterministic with it disabled.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = str(Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import ArtifactStore
from configs.config import POLICY_PACK
from input_reader import ReaderResult, read_input, read_json, read_web_form
from schemas.finding_schema import Finding, FindingStatus, Severity
from schemas.pr_schema import ExtractedField, ExtractedPR

SOURCE_AGENT = "Agent B"

# Head nouns that, on their own, make a 1-2 word description too vague to price.
_GENERIC_HEADS = {
    "equipment", "supplies", "supply", "items", "item", "goods", "material",
    "materials", "hardware", "software", "services", "service", "stuff",
    "accessories", "misc", "miscellaneous", "other", "things", "products",
}

# Field name -> caster for the value.
_STR = str
_FIELDS: Dict[str, Callable[[Any], Any]] = {
    "pr_id": _STR, "requester": _STR, "department": _STR, "cost_center": _STR,
    "vendor_name": _STR, "item_description": _STR, "item_category": _STR,
    "quantity": int, "unit_price": float, "estimated_amount": float,
    "currency": _STR, "business_justification": _STR, "urgency": _STR,
}


@dataclass
class AgentBResult:
    extracted_pr: ExtractedPR
    extracted_path: Path
    findings: List[Finding] = field(default_factory=list)
    llm_fallback_used: bool = False


# ---------- helpers ----------
def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _requisition_path(ares: AgentAResult) -> Path:
    for ev in ares.evidence_index.get("evidence", []):
        if ev.get("role") == "requisition":
            return Path(ev["path"])
    ctx = ares.context_packet
    return Path(ctx["bundle_dir"]) / ctx["requisition_file"]


def _find_value_bbox(pages: List[Any], raw: Any) -> Tuple[Optional[int], Optional[List[float]]]:
    """Best-effort: locate a value as a contiguous run of words on a page."""
    if not pages or raw is None:
        return None, None
    toks = str(raw).split()
    if not toks:
        return None, None
    for page in pages:
        words = page.words
        n = len(words)
        for i in range(n - len(toks) + 1):
            if all(words[i + k].text == toks[k] for k in range(len(toks))):
                boxes = [words[i + k].bbox for k in range(len(toks))]
                return page.page_number, [
                    min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes),
                ]
    return None, None


def _parse_pdf_fields(raw_text: str) -> Dict[str, Any]:
    """Fallback values parser for a PDF-only bundle (no JSON). Label-based."""
    label_map = {
        "pr id": "pr_id", "requester": "requester", "department": "department",
        "cost center": "cost_center", "vendor name": "vendor_name", "vendor": "vendor_name",
        "item description": "item_description", "item category": "item_category",
        "quantity": "quantity", "unit price": "unit_price",
        "estimated amount": "estimated_amount", "currency": "currency",
        "business justification": "business_justification", "urgency": "urgency",
    }
    extras = ["sole source", "emergency", "framework agreement", "blanket order",
              "simulated confidence score", "requested date", "number of bids"]
    all_labels = sorted(list(label_map) + extras, key=len, reverse=True)
    pattern = re.compile(r"(" + "|".join(re.escape(x) for x in all_labels) + r")\s*:\s*", re.IGNORECASE)
    matches = list(pattern.finditer(raw_text))
    out: Dict[str, Any] = {}
    for i, m in enumerate(matches):
        label = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        value = raw_text[start:end].strip()
        canon = label_map.get(label)
        if canon and canon not in out:
            out[canon] = value
    return out


def _is_vague(description: str) -> bool:
    desc = (description or "").strip()
    if len(desc) < 4:
        return True
    words = desc.split()
    if len(words) <= 2 and words[-1].lower() in _GENERIC_HEADS:
        return True
    return False


def _maybe_llm_normalize(text: str, llm_fallback: Optional[Callable[[str], str]]) -> Tuple[str, bool]:
    """Optional LLM fallback for a genuinely unclear (empty) field. Off by default.

    Never overrides a non-empty value and never makes decisions. Returns
    (possibly-normalized text, used_flag). No-op if disabled/unavailable.
    """
    if llm_fallback is not None:  # injected (tests)
        try:
            return llm_fallback(text), True
        except Exception:
            return text, False
    if os.environ.get("IPRMS_LLM_FALLBACK_ENABLED", "").lower() not in ("1", "true", "yes"):
        return text, False
    try:
        from llm.extraction_fallback import normalize_item_description  # optional module
        return normalize_item_description(text), True
    except Exception:
        return text, False


def _load_values_and_pages(ares: AgentAResult, req_path: Path) -> Tuple[Dict[str, Any], List[Any]]:
    """Return (values from JSON-first, pages for bbox)."""
    input_type = ares.context_packet["input_type"]

    if input_type == "json":
        reader = read_json(req_path)
        return dict(reader.fields or {}), []
    if input_type == "web_form":
        payload = json.loads(req_path.read_text(encoding="utf-8"))
        reader = read_web_form(payload)
        return dict(reader.fields or {}), []

    # pdf / scanned_pdf: PDF gives bounding boxes; values come from sibling JSON.
    reader: ReaderResult = read_input(req_path, input_type)
    json_sibling = req_path.with_suffix(".json")
    if json_sibling.exists():
        values = json.loads(json_sibling.read_text(encoding="utf-8"))
    else:
        values = _parse_pdf_fields(reader.raw_text)  # PDF-only fallback
    return values, reader.pages


# ---------- main entry ----------
def run(
    ares: AgentAResult,
    *,
    llm_fallback: Optional[Callable[[str], str]] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> AgentBResult:
    req_path = _requisition_path(ares)
    values, pages = _load_values_and_pages(ares, req_path)

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    conf_min = policy.get("tolerances", {}).get("extraction_confidence_minimum", 0.85)

    confidence = _to_float(
        values.get("simulated_confidence_score", values.get("confidence_score")),
        default=0.99,
    )

    # Optional LLM fallback ONLY for a genuinely unclear (empty) item description.
    llm_used = False
    if not str(values.get("item_description", "")).strip():
        suggestion, llm_used = _maybe_llm_normalize("", llm_fallback)
        if suggestion:
            values["item_description"] = suggestion

    # Build ExtractedPR: values from JSON, bboxes from PDF.
    extracted_fields: Dict[str, ExtractedField] = {}
    for name, caster in _FIELDS.items():
        raw = values.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            value = caster(0) if caster in (int, float) else ""
            extracted_fields[name] = ExtractedField(value=value, confidence=min(confidence, 0.5))
            continue
        page, bbox = _find_value_bbox(pages, raw)
        extracted_fields[name] = ExtractedField(
            value=caster(raw), confidence=confidence, source_page=page, bounding_box=bbox
        )

    extracted = ExtractedPR(confidence_score=confidence, **extracted_fields)

    # Findings (Unified Findings Schema).
    findings: List[Finding] = []
    if confidence < conf_min:
        findings.append(Finding(
            finding_id="F-B-001",
            finding_type="LOW_CONFIDENCE_EXTRACTION",
            severity=Severity.MEDIUM,
            confidence=1.0,
            message=f"Extraction confidence {confidence} is below minimum {conf_min}.",
            evidence=["extracted_pr.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Route to manual review.",
            status=FindingStatus.OPEN,
        ))
    item_desc = str(values.get("item_description", "")).strip()
    if _is_vague(item_desc):
        findings.append(Finding(
            finding_id=f"F-B-{len(findings) + 1:03d}",
            finding_type="VAGUE_ITEM_DESCRIPTION",
            severity=Severity.MEDIUM,
            confidence=0.9,
            message=f"Item description '{item_desc}' is too vague for reliable pricing.",
            evidence=["extracted_pr.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Request buyer clarification.",
            status=FindingStatus.OPEN,
        ))

    # Persist into the same run directory created by Agent A.
    store = ArtifactStore(ares.run_id, root=ares.run_dir.parent)
    path = store.write_json("extracted_pr.json", extracted.model_dump(mode="json"))

    return AgentBResult(
        extracted_pr=extracted,
        extracted_path=path,
        findings=findings,
        llm_fallback_used=llm_used,
    )