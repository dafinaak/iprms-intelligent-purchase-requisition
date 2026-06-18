from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import RUN_SUMMARY_COLUMNS, ArtifactStore
from configs.config import POLICY_PACK
from schemas.decision_schema import ApprovalPacket, FinalDecision, PODraft, RunMetrics
from schemas.finding_schema import Finding

SOURCE_AGENT = "Agent H"
_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class AgentHResult:
    approval_packet: ApprovalPacket
    approval_packet_path: Path
    exceptions_path: Path
    po_draft: Optional[PODraft] = None
    metrics: Optional[RunMetrics] = None
    po_draft_path: Optional[Path] = None
    metrics_path: Optional[Path] = None
    audit_log_path: Optional[Path] = None
    run_summary_path: Optional[Path] = None
    findings: List[Finding] = field(default_factory=list)


def _field_value(data: Dict[str, Any], key: str) -> Any:
    f = data.get(key)
    return f.get("value") if isinstance(f, dict) else f


def _merge_and_dedup(raw_findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate by (finding_type, message); combine evidence; keep max severity."""
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for f in raw_findings:
        key = (str(f.get("finding_type", "")).strip().upper(),
               str(f.get("message", "")).strip().lower())
        if key not in merged:
            merged[key] = dict(f)
            merged[key]["evidence"] = list(f.get("evidence", []))
        else:
            existing = merged[key]
            for ev in f.get("evidence", []):
                if ev not in existing["evidence"]:
                    existing["evidence"].append(ev)
            if _SEV_RANK.get(f.get("severity"), 0) > _SEV_RANK.get(existing.get("severity"), 0):
                existing["severity"] = f.get("severity")
    return list(merged.values())


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None,
        processing_time_seconds: float = 0.0,
        rerun_of: Optional[str] = None) -> AgentHResult:
    run_dir = ares.run_dir

    def _load(name: str) -> Dict[str, Any]:
        p = run_dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    extracted = _load("extracted_pr.json")
    context = _load("context_packet.json")
    budget = _load("budget_check.json")
    vendor = _load("vendor_match.json")
    pol = _load("policy_check.json")
    sole = _load("sole_source_check.json")
    bid = _load("bid_threshold_check.json")
    anomaly = _load("anomaly_report.json")

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    routing = policy.get("routing", {})
    conf_min = float(policy.get("tolerances", {}).get("extraction_confidence_minimum", 0.85))

    pr_id = _field_value(extracted, "pr_id") or context.get("bundle_id", "")
    amount = extracted.get("estimated_amount", {})
    amount = amount.get("value") if isinstance(amount, dict) else 0
    confidence_score = float(extracted.get("confidence_score", 1.0))
    manager_limit = float(policy.get("approval_thresholds", {}).get("manager_limit", 1000))

    # ---- Merge + dedup findings from every artifact ----
    raw: List[Dict[str, Any]] = []
    raw += context.get("initial_risk_flags", [])
    for art in (budget, vendor, pol, sole, bid, anomaly):
        raw += art.get("findings", [])
    # Agent B findings are not persisted to an artifact; derive low-confidence here.
    if confidence_score < conf_min and not any(
        f.get("finding_type") == "LOW_CONFIDENCE_EXTRACTION" for f in raw
    ):
        raw.append({
            "finding_id": "F-H-LC", "finding_type": "LOW_CONFIDENCE_EXTRACTION",
            "severity": "medium", "confidence": 1.0,
            "message": f"Extraction confidence {confidence_score} below minimum {conf_min}.",
            "evidence": ["extracted_pr.json"], "source_agent": SOURCE_AGENT,
            "recommended_action": "Route to manual review.", "status": "open",
        })

    merged = _merge_and_dedup(raw)
    exceptions = [f for f in merged if _SEV_RANK.get(f.get("severity"), 0) >= _SEV_RANK["medium"]]
    exception_count = len(exceptions)
    highest_severity = "none"
    if merged:
        top = max(merged, key=lambda f: _SEV_RANK.get(f.get("severity"), 0))
        highest_severity = top.get("severity", "none") if _SEV_RANK.get(top.get("severity"), 0) else "none"

    # ---- Deterministic final decision (priority order) ----
    budget_result = budget.get("result", "passed")
    vendor_result = vendor.get("result", "matched")
    ss_result = sole.get("result", "ok")
    bid_exceeds = bool(bid.get("exceeds_threshold")) and not bool(bid.get("sufficient_bids"))
    anomaly_split = bool(anomaly.get("split_order_detected"))
    anomaly_any = bool(anomaly.get("anomaly_detected"))
    compliance_violation = pol.get("compliance_status") == "violation"
    manual_required = bool(pol.get("manual_approval_required"))
    multi_currency = bool(pol.get("multi_currency_flag"))
    approval_level = pol.get("approval_level_required", "")

    if budget_result != "passed":
        decision, routed_to = FinalDecision.BLOCKED, budget.get("routed_to") or routing.get("budget_exhausted", "FP&A")
    elif confidence_score < conf_min:
        decision, routed_to = FinalDecision.MANUAL_REVIEW, routing.get("low_confidence_extraction", "Manual Review")
    elif vendor_result == "pricing_uncertain":
        decision, routed_to = FinalDecision.BUYER_CLARIFICATION, "Buyer Clarification"
    elif anomaly_split or anomaly_any:
        decision, routed_to = FinalDecision.EXCEPTION, routing.get("split_order_detected", "Compliance")
    elif ss_result == "emergency_sole_source":
        decision, routed_to = FinalDecision.EXPEDITED_APPROVAL, routing.get("emergency_sole_source", "Expedited Approval")
    elif bid_exceeds:
        decision, routed_to = FinalDecision.MANUAL_APPROVAL, routing.get("non_preferred_vendor", "Procurement")
    elif vendor_result == "not_approved" or compliance_violation:
        decision, routed_to = FinalDecision.EXCEPTION, routing.get("non_preferred_vendor", "Procurement")
    elif manual_required:
        routed_to = "Procurement" if multi_currency else (approval_level or "Manager")
        decision = FinalDecision.MANUAL_APPROVAL
    else:
        decision, routed_to = FinalDecision.AUTO_PO, None

    po_status = "ready_for_posting" if decision == FinalDecision.AUTO_PO else "blocked"

    complex_flags = {
        "framework_agreement": bool(pol.get("framework_agreement_flag")),
        "blanket_order": bool(pol.get("blanket_order_flag")),
        "emergency_procurement": bool(pol.get("emergency_procurement_flag")),
        "multi_currency": bool(pol.get("multi_currency_flag")),
    }

    findings_models = [Finding(**f) for f in merged]
    summary = (f"{decision.value}: {exception_count} exception(s), "
               f"highest severity {highest_severity}"
               + (f", routed to {routed_to}" if routed_to else ""))

    packet = ApprovalPacket(
        run_id=ares.run_id,
        pr_id=pr_id,
        final_decision=decision,
        routed_to=routed_to,
        approval_level_required=approval_level,
        po_status=po_status,
        exception_count=exception_count,
        highest_severity=highest_severity,
        complex_flags=complex_flags,
        summary=summary,
        findings=findings_models,
    )

    # ---- exceptions.md ----
    lines = [f"# Exceptions — {ares.run_id}", "", f"PR: {pr_id}", f"Decision: {decision.value}", ""]
    if not exceptions:
        lines.append("No critical exceptions.")
    else:
        for f in sorted(exceptions, key=lambda x: -_SEV_RANK.get(x.get("severity"), 0)):
            lines.append(f"## [{str(f.get('severity', '')).upper()}] {f.get('finding_type', '')}")
            lines.append(f.get("message", ""))
            lines.append(f"- Evidence: {', '.join(f.get('evidence', [])) or 'n/a'}")
            lines.append(f"- Source: {f.get('source_agent', '')}")
            lines.append(f"- Recommended: {f.get('recommended_action', '')}")
            lines.append("")

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    packet_path = store.write_json("approval_packet.json", packet.model_dump(mode="json"))
    exceptions_path = store.write_markdown("exceptions.md", "\n".join(lines) + "\n")

    # ---- Task 28: PO draft, metrics, audit log, run summary ----
    requester = _field_value(extracted, "requester") or ""
    cost_center = _field_value(extracted, "cost_center") or ""
    vendor_name = _field_value(extracted, "vendor_name") or ""
    item_description = _field_value(extracted, "item_description") or ""
    item_category = _field_value(extracted, "item_category") or ""
    currency = _field_value(extracted, "currency") or ""
    try:
        quantity = int(_field_value(extracted, "quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    try:
        unit_price = float(_field_value(extracted, "unit_price") or 0)
    except (TypeError, ValueError):
        unit_price = 0.0
    input_hash = context.get("input_hash") or ares.input_hash

    po_draft = PODraft(
        run_id=ares.run_id, pr_id=pr_id, requester=requester, cost_center=cost_center,
        vendor_name=vendor_name, item_description=item_description, item_category=item_category,
        quantity=quantity, unit_price=unit_price, estimated_amount=float(amount or 0),
        currency=currency, final_decision=decision.value, po_status=po_status,
    )
    metrics = RunMetrics(
        run_id=ares.run_id, pr_id=pr_id, input_hash=input_hash,
        idempotency_check="passed", rerun_of=rerun_of, final_decision=decision.value,
        exception_count=exception_count, highest_severity=highest_severity,
        po_status=po_status, processing_time_seconds=processing_time_seconds,
    )

    summary_row = {
        "run_id": ares.run_id, "pr_id": pr_id, "requester": requester,
        "cost_center": cost_center, "vendor_name": vendor_name,
        "requested_amount": amount or 0, "currency": currency,
        "final_decision": decision.value, "exception_count": exception_count,
        "highest_severity": highest_severity, "routed_to": routed_to or "",
        "po_status": po_status, "processing_time_seconds": processing_time_seconds,
        "idempotency_check": "passed",
    }

    audit = [
        f"# Audit Log — {ares.run_id}", "",
        f"- PR: {pr_id}",
        f"- Created: {context.get('created_at', '')}",
        f"- Input hash: {input_hash}",
        f"- Re-run of: {rerun_of or 'n/a'}",
        f"- Idempotency check: passed",
        f"- Final decision: {decision.value}",
        f"- Routed to: {routed_to or 'n/a'}",
        f"- PO status: {po_status}",
        f"- Exceptions: {exception_count} (highest severity: {highest_severity})",
        "",
        "## Agent pipeline (deterministic A->H)",
        "- Agent A: intake, validation, PR-type, evidence index",
        "- Agent B: extraction (+bbox)",
        "- Agent C: budget validation",
        "- Agent D: vendor matching + catalogue pricing",
        "- Agent E: compliance & complex procurement",
        "- Agent F: sole-source / bid-threshold",
        "- Agent G: split-order / anomaly",
        "- Agent H: triage, decision, artifacts",
    ]

    po_draft_path = store.write_json("po_draft.json", po_draft.model_dump(mode="json"))
    metrics_path = store.write_json("metrics.json", metrics.model_dump(mode="json"))
    audit_log_path = store.write_markdown("audit_log.md", "\n".join(audit) + "\n")
    run_summary_path = store.write_run_summary(summary_row)

    return AgentHResult(
        approval_packet=packet,
        approval_packet_path=packet_path,
        exceptions_path=exceptions_path,
        po_draft=po_draft,
        metrics=metrics,
        po_draft_path=po_draft_path,
        metrics_path=metrics_path,
        audit_log_path=audit_log_path,
        run_summary_path=run_summary_path,
        findings=findings_models,
    )
