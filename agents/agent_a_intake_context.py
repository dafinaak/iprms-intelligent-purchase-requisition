"""Agent A - Intake & Context / Gatekeeper (IPRMS).

Agent A is the entry point of the pipeline (plani.pdf §6). It:
  1. accepts a PR bundle and validates it (gatekeeper) — invalid bundles are
     blocked before any processing happens;
  2. detects the input type (PDF / scanned PDF / JSON / web form) from the
     manifest;
  3. generates a run_id and creates runs/<run_id>/;
  4. builds context_packet.json and evidence_index.json (its mandatory outputs);
  5. performs initial, intake-level risk filtering, reported as Findings in the
     Unified Findings Schema so Agent H can later merge them.

Standalone & deterministic: Agent A reads the manifest and the bundle files only.
It does NOT parse the requisition document — that is Agent B's job — so it stays
decoupled from the extraction/OCR layer (input_reader.py).
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the repo root importable when this module is run directly as a script
# (python agents/agent_a_intake_context.py). Tests use conftest.py instead.
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

SOURCE_AGENT = "Agent A"


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
    """RUN-<UTC timestamp>-<input_hash prefix>.

    The timestamp keeps run_ids unique across runs; the hash prefix ties the id to
    the inputs and aids quick visual idempotency checks.
    """
    when = when or datetime.now(timezone.utc)
    return f"RUN-{when:%Y%m%dT%H%M%SZ}-{input_hash[:8]}"


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
    """Lightweight, intake-level risk flags — no extraction is performed here.

    These are early signals (evidence-first) that downstream agents and Agent H
    can act on; they never decide approval/routing themselves.
    """
    findings: List[Finding] = []

    def _next_id() -> str:
        return f"F-A-{len(findings) + 1:03d}"

    # Input-type risk: scanned PDFs depend on OCR and tend to extract with lower
    # confidence; flag so a low-confidence/manual-review path is possible later.
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

    # Data-integrity risk: any zero-byte evidence file means a supporting input is
    # effectively missing even though the file exists.
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
) -> AgentAResult:
    """Run Agent A on a PR bundle.

    Gatekeeper: the bundle is validated first; an invalid bundle raises
    ManifestValidationError and NO run directory is created. On success the run
    directory and both mandatory artifacts are written.

    Args:
        bundle_dir: path to the PR bundle (folder containing manifest.yaml).
        run_id: optional explicit run id (deterministic runs / tests). If omitted
            it is generated from the timestamp + input hash.
        runs_root: root under which runs/<run_id>/ is created.
        policy_pack: policy_pack.yaml, folded into the input hash (§13).
        when: optional fixed timestamp for deterministic run_id generation.
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

    # 3. Create runs/<run_id>/.
    store = ArtifactStore(run_id, root=runs_root)

    # 4. Evidence index (mandatory output).
    evidence_index = _build_evidence_index(run_id, manifest, result)

    # 5. Initial risk filtering.
    findings = _initial_risk_filter(input_type, evidence_index)

    # 6. Context packet (mandatory output).
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
        "initial_risk_flags": [f.model_dump(mode="json") for f in findings],
        "source_agent": SOURCE_AGENT,
    }

    # Persist mandatory artifacts.
    store.write_json("context_packet.json", context_packet)
    store.write_json("evidence_index.json", evidence_index)

    return AgentAResult(
        run_id=run_id,
        run_dir=store.run_dir,
        input_type=input_type,
        input_hash=input_hash,
        context_packet=context_packet,
        evidence_index=evidence_index,
        findings=findings,
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

    print(f"[Agent A] run_id={res.run_id}")
    print(f"[Agent A] run_dir={res.run_dir}")
    print(f"[Agent A] input_type={res.input_type}  input_hash={res.input_hash[:12]}...")
    print(f"[Agent A] evidence files indexed: {res.evidence_index['evidence_count']}")
    print(f"[Agent A] initial risk flags: {len(res.findings)}")
    for f in res.findings:
        print(f"          - [{f.severity.value}] {f.finding_type}: {f.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
