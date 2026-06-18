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
from schemas.artifact_schema import PolicyCheck
from schemas.finding_schema import Finding, FindingStatus, Severity

SOURCE_AGENT = "Agent E"


@dataclass
class AgentEResult:
    policy_check: PolicyCheck
    policy_check_path: Path
    findings: List[Finding] = field(default_factory=list)


def _field_value(data: Dict[str, Any], key: str) -> Any:
    f = data.get(key)
    return f.get("value") if isinstance(f, dict) else f


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _evidence_path(ares: AgentAResult, role: str) -> Path:
    for ev in ares.evidence_index.get("evidence", []):
        if ev.get("role") == role:
            return Path(ev["path"])
    raise KeyError(f"evidence role not found: {role}")


def _load_metadata(ares: AgentAResult) -> Dict[str, Any]:
    """Structured requisition metadata (framework_agreement / blanket_order / emergency)."""
    req = _evidence_path(ares, "requisition")
    candidate = req if req.suffix.lower() == ".json" else req.with_suffix(".json")
    if candidate.exists():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _approval_level(amount: float, thresholds: Dict[str, Any]) -> str:
    manager = _to_float(thresholds.get("manager_limit"), 1000)
    finance = _to_float(thresholds.get("finance_limit"), 5000)
    director = _to_float(thresholds.get("director_limit"), 10000)
    if amount <= manager:
        return "Manager"
    if amount <= finance:
        return "Finance"
    if amount <= director:
        return "Director"
    return "Board"


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None) -> AgentEResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))

    def _load(name: str) -> Dict[str, Any]:
        p = run_dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    budget = _load("budget_check.json")
    vendor = _load("vendor_match.json")

    pr_id = _field_value(extracted, "pr_id") or ""
    amount = _to_float(_field_value(extracted, "estimated_amount"))
    urgency = str(_field_value(extracted, "urgency") or "")
    currency = str(_field_value(extracted, "currency") or "")
    meta = _load_metadata(ares)

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    thresholds = policy.get("approval_thresholds", {})
    manager_limit = _to_float(thresholds.get("manager_limit"), 1000)

    # Inputs from upstream agents.
    vendor_result = vendor.get("result", "matched")
    justification_present = bool(vendor.get("justification_present", False))
    budget_result = budget.get("result", "passed")
    budget_ok = budget_result == "passed"
    non_preferred = vendor_result == "non_preferred"
    vendor_not_approved = vendor_result == "not_approved"

    approval_level = _approval_level(amount, thresholds)

    findings: List[Finding] = []
    violations: List[str] = []

    # Non-preferred vendor rule: exception only when no written justification.
    if non_preferred and not justification_present:
        violations.append("non_preferred_vendor_without_justification")
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="POLICY_VIOLATION_NON_PREFERRED_VENDOR",
            severity=Severity.HIGH, confidence=0.95,
            message="Non-preferred vendor used without written justification.",
            evidence=["policy_check.json", "vendor_match.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Manual approval required (Procurement).",
            status=FindingStatus.OPEN,
        ))
    if vendor_not_approved:
        violations.append("vendor_not_approved")
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="POLICY_VIOLATION_VENDOR_NOT_APPROVED",
            severity=Severity.HIGH, confidence=0.95,
            message="Requested vendor is not approved.",
            evidence=["policy_check.json", "vendor_match.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Manual approval required (Procurement).",
            status=FindingStatus.OPEN,
        ))
    # Budget policy.
    if not budget_ok:
        violations.append("budget")
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="POLICY_VIOLATION_BUDGET",
            severity=Severity.HIGH, confidence=0.95,
            message=f"Budget policy failed (budget_check result={budget_result}).",
            evidence=["policy_check.json", "budget_check.json"],
            source_agent=SOURCE_AGENT,
            recommended_action="Route per budget policy (FP&A).",
            status=FindingStatus.OPEN,
        ))

    # ---- Complex procurement checks (Task 24) ----
    complex_cfg = policy.get("complex_procurement", {})
    mc_cfg = policy.get("multi_currency", {})
    default_currency = policy.get("regional_rules", {}).get("default_currency", "EUR")

    framework_agreement_flag = bool(meta.get("framework_agreement")) and bool(
        complex_cfg.get("framework_agreements_enabled", True))
    blanket_order_flag = bool(meta.get("blanket_order")) and bool(
        complex_cfg.get("blanket_orders_enabled", True))
    emergency_procurement_flag = bool(meta.get("emergency")) or urgency.strip().lower() == "emergency"
    multi_currency_flag = bool(complex_cfg.get("multi_currency_pos_enabled", True)) and bool(
        currency) and currency != default_currency

    requires_currency_approval = multi_currency_flag and bool(mc_cfg.get("requires_approval_if_foreign", True))

    if framework_agreement_flag:
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="FRAMEWORK_AGREEMENT", severity=Severity.LOW, confidence=0.9,
            message="PR falls under a framework agreement; policy-driven handling.",
            evidence=["policy_check.json"], source_agent=SOURCE_AGENT,
            recommended_action="Apply configured framework-agreement routing.",
            status=FindingStatus.OPEN,
        ))
    if blanket_order_flag:
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="BLANKET_ORDER", severity=Severity.LOW, confidence=0.9,
            message="PR falls under a blanket order; policy-driven handling.",
            evidence=["policy_check.json"], source_agent=SOURCE_AGENT,
            recommended_action="Apply configured blanket-order routing.",
            status=FindingStatus.OPEN,
        ))
    if emergency_procurement_flag:
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="EMERGENCY_PROCUREMENT", severity=Severity.MEDIUM, confidence=0.9,
            message="Emergency procurement; expedited approval path required.",
            evidence=["policy_check.json"], source_agent=SOURCE_AGENT,
            recommended_action="Route to expedited approval.",
            status=FindingStatus.OPEN,
        ))
    if requires_currency_approval:
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="MULTI_CURRENCY", severity=Severity.MEDIUM, confidence=0.9,
            message=f"Foreign currency {currency} (default {default_currency}) requires approval.",
            evidence=["policy_check.json"], source_agent=SOURCE_AGENT,
            recommended_action="Route for multi-currency approval.",
            status=FindingStatus.OPEN,
        ))

    manual_approval_required = (
        bool(violations) or amount > manager_limit
        or emergency_procurement_flag or requires_currency_approval
    )
    compliance_status = "violation" if violations else "compliant"

    if manual_approval_required and not violations:
        findings.append(Finding(
            finding_id=f"F-E-{len(findings) + 1:03d}",
            finding_type="APPROVAL_REQUIRED",
            severity=Severity.LOW, confidence=0.9,
            message=f"Amount {amount} exceeds manager limit {manager_limit}; {approval_level} approval required.",
            evidence=["policy_check.json"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {approval_level} for approval.",
            status=FindingStatus.OPEN,
        ))

    policy_check = PolicyCheck(
        pr_id=pr_id,
        compliance_status=compliance_status,
        approval_level_required=approval_level,
        manual_approval_required=manual_approval_required,
        non_preferred_vendor=non_preferred,
        vendor_approved=not vendor_not_approved,
        budget_ok=budget_ok,
        urgency=urgency,
        violations=violations,
        framework_agreement_flag=framework_agreement_flag,
        blanket_order_flag=blanket_order_flag,
        emergency_procurement_flag=emergency_procurement_flag,
        multi_currency_flag=multi_currency_flag,
        findings=findings,
    )

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    path = store.write_json("policy_check.json", policy_check.model_dump(mode="json"))

    return AgentEResult(policy_check=policy_check, policy_check_path=path, findings=findings)
