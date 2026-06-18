"""Agent A - Intake & Context / Gatekeeper (IPRMS).

Agent A is the entry point of the pipeline (plani.pdf §6). It:
  1. accepts a PR bundle and validates it (gatekeeper) — invalid bundles are
     blocked before any processing happens;
  2. detects the input type (PDF / scanned PDF / JSON / web form) from the
     manifest;
  3. classifies the PR type (standard / emergency / capex) from deterministic
     metadata first; a controlled LLM fallback may only SUGGEST a type when the
     metadata is insufficient (§4.1) and never decides routing/approval/budget/
     vendor/compliance/anomaly/PO rules;
  4. generates a run_id and creates runs/<run_id>/;
  5. builds context_packet.json and evidence_index.json (mandatory outputs), plus
     llm_fallback_trace.json only when the Agent A fallback is used;
  6. performs initial, intake-level risk filtering as Findings.

Standalone & deterministic: Agent A reads the manifest, the bundle files, and the
structured PR metadata (requisition JSON) only. It does NOT OCR/parse the PDF
document — that is Agent B's job. With the LLM fallback disabled, classification
is fully deterministic.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from artifact_store import RUNS_ROOT, ArtifactStore
from configs.config import POLICY_PACK
from manifest_validation import (
    Manifest,
    ManifestValidationError,
    ValidationResult,
    validate_bundle,
)
from schemas.finding_schema import Finding, FindingStatus, Severity
from schemas.pr_schema import (
    ClassificationSource,
    LLMFallbackTrace,
    PRType,
    PRTypeClassification,
)

SOURCE_AGENT = "Agent A"

# Fallback defaults used only if policy_pack has no `pr_classification` section.
# The authoritative rules live in configs/policy_pack.yaml -> pr_classification.
_DEFAULT_CAPEX_CATEGORIES = {
    "capital equipment", "machinery", "vehicle", "infrastructure",
    "capex", "asset", "building", "capital",
}
_DEFAULT_EMERGENCY_URGENCIES = {"emergency", "urgent", "critical"}
_DEFAULT_CAPEX_THRESHOLD = 10000.0


@dataclass
class AgentAResult:
    """Everything Agent A produces, returned to the pipeline runner."""

    run_id: str
    run_dir: Path
    input_type: str
    input_hash: str
    context_packet: Dict[str, Any]
    evidence_index: Dict[str, Any]
    findings: List[Finding] = field(default_factory=list)
    classification: Optional[PRTypeClassification] = None
    llm_fallback_used: bool = False


# ---------- helpers ----------
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_input_hash(result: ValidationResult, policy_pack: Path) -> str:
    """Deterministic hash of the inputs that define a run's identity.

    Per plani.pdf §13: hash from the PR bundle (requisition + supporting files) +
    manifest.yaml + policy_pack.yaml. Files are folded in sorted-key order so the
    hash is stable regardless of dict ordering. Used downstream for idempotency.
    """
    manifest = result.manifest
    assert manifest is not None  # caller only invokes this for a valid bundle

    files: Dict[str, Path] = {
        "manifest.yaml": result.requisition_path.parent / "manifest.yaml",
        "requisition::" + manifest.requisition_file: result.requisition_path,
    }
    for role, p in result.supporting_paths.items():
        files[f"support::{role}"] = p
    if policy_pack.exists():
        files["policy_pack.yaml"] = policy_pack

    h = hashlib.sha256()
    for key in sorted(files):
        p = files[key]
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        if p.exists():
            h.update(_sha256_file(p).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _generate_run_id(input_hash: str, when: Optional[datetime] = None) -> str:
    """RUN-<UTC timestamp>-<input_hash prefix>."""
    when = when or datetime.now(timezone.utc)
    return f"RUN-{when:%Y%m%dT%H%M%SZ}-{input_hash[:8]}"


def _load_metadata(result: ValidationResult) -> Dict[str, Any]:
    """Load structured PR metadata (the requisition JSON) for classification.

    This is metadata only — NOT document OCR/parsing (Agent B's job). Returns {}
    when no structured metadata is available (e.g. a PDF-only bundle, no JSON).
    """
    req = Path(result.requisition_path)
    candidate = req if req.suffix.lower() == ".json" else req.with_suffix(".json")
    if candidate.exists():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def classify_pr_type_metadata(
    metadata: Dict[str, Any], policy: Dict[str, Any]
) -> Tuple[Optional[PRType], float]:
    """Deterministic PR-type classification from metadata.

    Returns (PRType, confidence) or (None, 0.0) when metadata is insufficient.
    """
    if not metadata:
        return None, 0.0

    # Rules from policy_pack -> pr_classification (config-driven), with fallbacks.
    cfg = policy.get("pr_classification", {})
    emergency_urgencies = {str(u).strip().lower()
                           for u in cfg.get("emergency_urgencies", _DEFAULT_EMERGENCY_URGENCIES)}
    capex_categories = {str(c).strip().lower()
                        for c in cfg.get("capex_categories", _DEFAULT_CAPEX_CATEGORIES)}
    capex_threshold = float(cfg.get(
        "capex_amount_threshold",
        policy.get("approval_thresholds", {}).get("director_limit", _DEFAULT_CAPEX_THRESHOLD),
    ))

    urgency = str(metadata.get("urgency", "")).strip().lower()
    if bool(metadata.get("emergency")) or urgency in emergency_urgencies:
        return PRType.EMERGENCY, 0.95

    category = str(metadata.get("item_category", "")).strip().lower()
    try:
        amount = float(metadata.get("estimated_amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if category in capex_categories or amount >= capex_threshold:
        return PRType.CAPEX, 0.9

    if category or urgency:
        return PRType.STANDARD, 0.9
    return None, 0.0  # not enough signal


def _maybe_llm_pr_type(
    metadata: Dict[str, Any], classifier: Optional[Callable[[Dict[str, Any]], Any]]
) -> Tuple[Optional[PRType], bool, Optional[str]]:
    """Controlled LLM fallback for PR-type. Off by default; returns (type, used, model).

    The LLM may only SUGGEST a type. No-op (returns None, False, None) when
    disabled or unavailable, keeping classification deterministic.
    """
    def _coerce(val: Any) -> Optional[PRType]:
        try:
            return val if isinstance(val, PRType) else PRType(str(val).strip().lower())
        except Exception:
            return None

    if classifier is not None:  # injected (tests)
        try:
            return _coerce(classifier(metadata)), True, "injected"
        except Exception:
            return None, False, None

    # Delegate to the controlled llm/ module (returns None when disabled/unavailable).
    try:
        from llm.pr_type_classifier import classify_pr_type
        candidate = classify_pr_type(metadata)
        if candidate is None:
            return None, False, None
        return _coerce(candidate), True, "llm"
    except Exception:
        return None, False, None


def _build_evidence_index(
    run_id: str, manifest: Manifest, result: ValidationResult
) -> Dict[str, Any]:
    """Index every evidence file in the bundle for downstream agents."""
    entries: List[Dict[str, Any]] = []

    def _entry(role: str, fname: str, path: Path) -> Dict[str, Any]:
        exists = path.exists()
        return {
            "evidence_id": f"E-{len(entries) + 1:03d}",
            "role": role,
            "file": fname,
            "path": str(path),
            "exists": exists,
            "size_bytes": path.stat().st_size if exists else 0,
            "sha256": _sha256_file(path) if exists else None,
        }

    entries.append(_entry("requisition", manifest.requisition_file, result.requisition_path))
    for role in sorted(result.supporting_paths):
        fname = manifest.supporting_files[role]
        entries.append(_entry(role, fname, result.supporting_paths[role]))

    return {
        "run_id": run_id,
        "bundle_id": manifest.bundle_id,
        "evidence_count": len(entries),
        "evidence": entries,
        "source_agent": SOURCE_AGENT,
    }


def _initial_risk_filter(
    input_type: str, evidence_index: Dict[str, Any]
) -> List[Finding]:
    """Lightweight, intake-level risk flags — no extraction is performed here."""
    findings: List[Finding] = []

    def _next_id() -> str:
        return f"F-A-{len(findings) + 1:03d}"

    if input_type == "scanned_pdf":
        findings.append(Finding(
            finding_id=_next_id(),
            finding_type="OCR_REQUIRED",
            severity=Severity.MEDIUM,
            confidence=0.9,
            message="Input is a scanned PDF; extraction relies on OCR and may be low confidence.",
            evidence=["evidence_index.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Expect possible low-confidence extraction; allow manual review fallback.",
            status=FindingStatus.OPEN,
        ))
    elif input_type == "pdf":
        findings.append(Finding(
            finding_id=_next_id(),
            finding_type="PDF_EXTRACTION_REQUIRED",
            severity=Severity.LOW,
            confidence=0.8,
            message="Input is a digital PDF; fields will be parsed by Agent B (bounding boxes expected).",
            evidence=["evidence_index.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Proceed to extraction; verify per-field confidence downstream.",
            status=FindingStatus.OPEN,
        ))

    for ev in evidence_index["evidence"]:
        if ev["exists"] and ev["size_bytes"] == 0:
            findings.append(Finding(
                finding_id=_next_id(),
                finding_type="EMPTY_EVIDENCE_FILE",
                severity=Severity.HIGH,
                confidence=1.0,
                message=f"Evidence file '{ev['file']}' ({ev['role']}) is empty.",
                evidence=["evidence_index.json"],
                source_agent=SOURCE_AGENT,
                recommended_action="Reject or request a corrected PR bundle.",
                status=FindingStatus.OPEN,
            ))

    return findings


# ---------- main entry ----------
def run(
    bundle_dir: Path | str,
    *,
    run_id: Optional[str] = None,
    runs_root: Path | str = RUNS_ROOT,
    policy_pack: Path | str = POLICY_PACK,
    when: Optional[datetime] = None,
    pr_type_classifier: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> AgentAResult:
    """Run Agent A on a PR bundle.

    Gatekeeper: the bundle is validated first; an invalid bundle raises
    ManifestValidationError and NO run directory is created.
    """
    bundle_dir = Path(bundle_dir)
    policy_pack = Path(policy_pack)

    # 1. Gatekeeper — block invalid bundles before doing anything else.
    result = validate_bundle(bundle_dir)
    if not result.is_valid:
        raise ManifestValidationError(result)
    manifest = result.manifest
    assert manifest is not None

    input_type = manifest.input_type

    # 2. Identity: deterministic input hash, then a run_id.
    input_hash = _compute_input_hash(result, policy_pack)
    run_id = run_id or _generate_run_id(input_hash, when)
    store = ArtifactStore(run_id, root=runs_root)

    # 3. PR-type classification — deterministic metadata first, controlled LLM fallback.
    import yaml
    policy: Dict[str, Any] = {}
    if policy_pack.exists():
        policy = yaml.safe_load(policy_pack.read_text(encoding="utf-8")) or {}
    metadata = _load_metadata(result)

    pr_type, conf = classify_pr_type_metadata(metadata, policy)
    trace: Optional[LLMFallbackTrace] = None
    if pr_type is not None:
        source = ClassificationSource.METADATA
        llm_used = False
    else:
        candidate, llm_used, model = _maybe_llm_pr_type(metadata, pr_type_classifier)
        if llm_used and candidate is not None:
            pr_type, source, conf = candidate, ClassificationSource.LLM, 0.7
            trace = LLMFallbackTrace(
                source_agent=SOURCE_AGENT,
                fallback_type="pr_type_classification",
                used=True,
                reason="PR metadata insufficient to classify standard/emergency/capex.",
                confidence=conf,
                model=model,
                normalized_candidate=pr_type.value,
                original_evidence="context_packet.json",
            )
        else:
            pr_type, source, conf = PRType.STANDARD, ClassificationSource.DEFAULT, 0.5

    classification = PRTypeClassification(
        pr_type=pr_type, source=source, confidence=conf, llm_fallback_used=llm_used
    )

    # 4. Evidence index + initial risk filtering.
    evidence_index = _build_evidence_index(run_id, manifest, result)
    findings = _initial_risk_filter(input_type, evidence_index)

    # 5. Context packet (mandatory output).
    created_at = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    context_packet: Dict[str, Any] = {
        "run_id": run_id,
        "bundle_id": manifest.bundle_id,
        "bundle_dir": str(bundle_dir.resolve()),
        "input_type": input_type,
        "requisition_file": manifest.requisition_file,
        "supporting_files": dict(manifest.supporting_files),
        "input_hash": input_hash,
        "created_at": created_at,
        "gatekeeper": {"manifest_valid": True, "errors": []},
        "pr_type": classification.pr_type.value,
        "pr_type_source": classification.source.value,
        "pr_type_confidence": classification.confidence,
        "llm_fallback_used": classification.llm_fallback_used,
        "llm_fallback_trace": "llm_fallback_trace.json" if trace is not None else None,
        "initial_risk_flags": [f.model_dump(mode="json") for f in findings],
        "source_agent": SOURCE_AGENT,
    }

    # 6. Persist artifacts (trace only when the fallback actually ran).
    store.write_json("context_packet.json", context_packet)
    store.write_json("evidence_index.json", evidence_index)
    if trace is not None:
        store.write_json("llm_fallback_trace.json", trace.model_dump(mode="json"))

    return AgentAResult(
        run_id=run_id,
        run_dir=store.run_dir,
        input_type=input_type,
        input_hash=input_hash,
        context_packet=context_packet,
        evidence_index=evidence_index,
        findings=findings,
        classification=classification,
        llm_fallback_used=classification.llm_fallback_used,
    )


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run IPRMS Agent A (Intake & Context / Gatekeeper).")
    parser.add_argument("--bundle", required=True, help="Path to the PR bundle folder.")
    parser.add_argument("--run-id", default=None, help="Optional explicit run id.")
    args = parser.parse_args(argv)

    try:
        res = run(args.bundle, run_id=args.run_id)
    except ManifestValidationError as e:
        print(f"[Agent A] BLOCKED - {e}")
        return 1

    c = res.classification
    print(f"[Agent A] run_id={res.run_id}")
    print(f"[Agent A] run_dir={res.run_dir}")
    print(f"[Agent A] input_type={res.input_type}  input_hash={res.input_hash[:12]}...")
    print(f"[Agent A] pr_type={c.pr_type.value} (source={c.source.value}, conf={c.confidence})")
    print(f"[Agent A] llm_fallback_used={res.llm_fallback_used}")
    print(f"[Agent A] evidence files indexed: {res.evidence_index['evidence_count']}")
    print(f"[Agent A] initial risk flags: {len(res.findings)}")
    for f in res.findings:
        print(f"          - [{f.severity.value}] {f.finding_type}: {f.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
