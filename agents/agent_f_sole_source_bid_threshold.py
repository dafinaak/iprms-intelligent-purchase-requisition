from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import ArtifactStore
from configs.config import POLICY_PACK
from schemas.artifact_schema import BidThresholdCheck, SoleSourceCheck
from schemas.finding_schema import Finding, FindingStatus, Severity

SOURCE_AGENT = "Agent F"


@dataclass
class AgentFResult:
    sole_source_check: SoleSourceCheck
    bid_threshold_check: BidThresholdCheck
    sole_source_path: Path
    bid_threshold_path: Path
    findings: List[Finding] = field(default_factory=list)


def _field_value(data: Dict[str, Any], key: str) -> Any:
    f = data.get(key)
    return f.get("value") if isinstance(f, dict) else f


def _evidence_path(ares: AgentAResult, role: str) -> Path:
    for ev in ares.evidence_index.get("evidence", []):
        if ev.get("role") == role:
            return Path(ev["path"])
    raise KeyError(f"evidence role not found: {role}")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_metadata(ares: AgentAResult) -> Dict[str, Any]:
    """Structured requisition metadata (sole_source/emergency/number_of_bids)."""
    req = _evidence_path(ares, "requisition")
    candidate = req if req.suffix.lower() == ".json" else req.with_suffix(".json")
    if candidate.exists():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None) -> AgentFResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))
    meta = _load_metadata(ares)

    pr_id = _field_value(extracted, "pr_id") or ""
    amount = _to_float(_field_value(extracted, "estimated_amount"))
    justification = str(_field_value(extracted, "business_justification") or "").strip()

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    bid_rules = policy.get("bid_rules", {})
    threshold = _to_float(bid_rules.get("threshold_amount"), 5000)
    required_bids = _to_int(bid_rules.get("minimum_required_bids"), 3)
    expedited_route = policy.get("routing", {}).get("emergency_sole_source", "Expedited Approval")

    bids_attached = _to_int(meta.get("number_of_bids"), 0)
    emergency = bool(meta.get("emergency"))
    justification_present = bool(justification)

    findings: List[Finding] = []

    # ---- Sole-source ----
    sole_source = bool(meta.get("sole_source")) or bids_attached <= 1
    emergency_sole_source = sole_source and emergency
    expedited_approval_required = emergency_sole_source

    if emergency_sole_source:
        ss_result = "emergency_sole_source"
        findings.append(Finding(
            finding_id=f"F-F-{len(findings) + 1:03d}",
            finding_type="EMERGENCY_SOLE_SOURCE",
            severity=Severity.MEDIUM, confidence=0.95,
            message="Emergency sole-source purchase detected; expedited approval path required.",
            evidence=["sole_source_check.json"], source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {expedited_route}.", status=FindingStatus.OPEN,
        ))
    elif sole_source:
        ss_result = "sole_source_detected"
        if not justification_present:
            findings.append(Finding(
                finding_id=f"F-F-{len(findings) + 1:03d}",
                finding_type="SOLE_SOURCE",
                severity=Severity.HIGH, confidence=0.95,
                message="Sole-source purchase without written justification.",
                evidence=["sole_source_check.json"], source_agent=SOURCE_AGENT,
                recommended_action="Require justification or competitive bids.",
                status=FindingStatus.OPEN,
            ))
        else:
            findings.append(Finding(
                finding_id=f"F-F-{len(findings) + 1:03d}",
                finding_type="SOLE_SOURCE",
                severity=Severity.LOW, confidence=0.9,
                message="Sole-source purchase with written justification.",
                evidence=["sole_source_check.json"], source_agent=SOURCE_AGENT,
                recommended_action="Document justification.", status=FindingStatus.OPEN,
            ))
    else:
        ss_result = "ok"

    sole_source_check = SoleSourceCheck(
        pr_id=pr_id,
        sole_source=sole_source,
        justification_present=justification_present,
        emergency=emergency,
        expedited_approval_required=expedited_approval_required,
        result=ss_result,
    )

    # ---- Bid threshold ----
    exceeds_threshold = amount > threshold
    sufficient_bids = bids_attached >= required_bids
    bid_findings: List[Finding] = []
    if exceeds_threshold and not sufficient_bids:
        bid_findings.append(Finding(
            finding_id="F-F-BID-001",
            finding_type="BID_THRESHOLD_EXCEEDED",
            severity=Severity.HIGH, confidence=0.95,
            message=(f"Amount {amount} exceeds bid threshold {threshold} with only "
                     f"{bids_attached}/{required_bids} required bids."),
            evidence=["bid_threshold_check.json"], source_agent=SOURCE_AGENT,
            recommended_action="Require competitive bids before approval.",
            status=FindingStatus.OPEN,
        ))

    bid_threshold_check = BidThresholdCheck(
        pr_id=pr_id,
        amount=amount,
        bid_threshold=threshold,
        exceeds_threshold=exceeds_threshold,
        required_bids=required_bids,
        bids_attached=bids_attached,
        sufficient_bids=sufficient_bids,
        result="threshold_exceeded" if exceeds_threshold else "ok",
        findings=bid_findings,
    )
    sole_source_check.findings = findings

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    ss_path = store.write_json("sole_source_check.json", sole_source_check.model_dump(mode="json"))
    bt_path = store.write_json("bid_threshold_check.json", bid_threshold_check.model_dump(mode="json"))

    return AgentFResult(
        sole_source_check=sole_source_check,
        bid_threshold_check=bid_threshold_check,
        sole_source_path=ss_path,
        bid_threshold_path=bt_path,
        findings=findings + bid_findings,
    )
