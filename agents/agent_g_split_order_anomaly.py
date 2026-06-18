from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import ArtifactStore
from configs.config import POLICY_PACK
from schemas.artifact_schema import AnomalyReport
from schemas.finding_schema import Finding, FindingStatus, Severity

SOURCE_AGENT = "Agent G"
SPLIT_MIN_TOTAL = 3   # same item across >= 3 PRs (incl. current) -> split order
WEEK_MIN_TOTAL = 3    # same dept/week across >= 3 PRs (incl. current) -> anomaly


@dataclass
class AgentGResult:
    anomaly_report: AnomalyReport
    anomaly_report_path: Path
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


def _norm(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _iso_week(value: Any):
    try:
        d = date.fromisoformat(str(value).strip())
        return d.isocalendar()[:2]  # (ISO year, ISO week)
    except (TypeError, ValueError):
        return None


def _load_metadata(ares: AgentAResult) -> Dict[str, Any]:
    req = _evidence_path(ares, "requisition")
    candidate = req if req.suffix.lower() == ".json" else req.with_suffix(".json")
    if candidate.exists():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None) -> AgentGResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))
    meta = _load_metadata(ares)

    pr_id = _field_value(extracted, "pr_id") or ""
    department = _field_value(extracted, "department") or ""
    cost_center = _field_value(extracted, "cost_center") or ""
    item = _field_value(extracted, "item_description") or ""
    amount = _to_float(_field_value(extracted, "estimated_amount"))
    requested_date = str(meta.get("requested_date") or "")

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    # Threshold avoidance uses the bid threshold from policy (not hardcoded).
    threshold = _to_float(policy.get("bid_rules", {}).get("threshold_amount"), 5000)
    split_route = policy.get("routing", {}).get("split_order_detected", "Compliance")

    history = list(csv.DictReader(_evidence_path(ares, "historical_prs").open(encoding="utf-8")))

    cur_week = _iso_week(requested_date)
    dept_norm = _norm(department)
    item_norm = _norm(item)

    same_dept_week = [
        r for r in history
        if _norm(r.get("department")) == dept_norm
        and cur_week is not None and _iso_week(r.get("pr_date")) == cur_week
    ]
    same_item = [r for r in history if _norm(r.get("item_description")) == item_norm]

    same_dept_week_total = len(same_dept_week) + 1   # include current
    same_item_total = len(same_item) + 1             # include current

    week_combined = sum(_to_float(r.get("amount")) for r in same_dept_week) + amount
    item_combined = sum(_to_float(r.get("amount")) for r in same_item) + amount

    split_order_detected = same_item_total >= SPLIT_MIN_TOTAL
    multiple_same_week = same_dept_week_total >= WEEK_MIN_TOTAL
    # Threshold avoidance: each PR is under threshold but the combined total reaches it.
    combined_amount = max(week_combined, item_combined)
    threshold_avoidance = (amount < threshold) and (combined_amount >= threshold) and (
        same_dept_week_total >= 2 or same_item_total >= 2
    )

    anomaly_detected = multiple_same_week or threshold_avoidance or split_order_detected

    findings: List[Finding] = []

    if split_order_detected:
        findings.append(Finding(
            finding_id=f"F-G-{len(findings) + 1:03d}",
            finding_type="SPLIT_ORDER",
            severity=Severity.HIGH, confidence=0.95,
            message=(f"Same item '{item}' appears across {same_item_total} PRs "
                     f"(combined {item_combined})."),
            evidence=["anomaly_report.json", "historical_prs.csv"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {split_route} for split-order review.",
            status=FindingStatus.OPEN,
        ))
    if multiple_same_week:
        findings.append(Finding(
            finding_id=f"F-G-{len(findings) + 1:03d}",
            finding_type="SAME_WEEK_MULTIPLE_PRS",
            severity=Severity.MEDIUM, confidence=0.9,
            message=(f"{same_dept_week_total} PRs from '{department}' in the same week "
                     f"(combined {week_combined})."),
            evidence=["anomaly_report.json", "historical_prs.csv"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {split_route} for review.",
            status=FindingStatus.OPEN,
        ))
    if threshold_avoidance:
        findings.append(Finding(
            finding_id=f"F-G-{len(findings) + 1:03d}",
            finding_type="THRESHOLD_AVOIDANCE",
            severity=Severity.HIGH, confidence=0.9,
            message=(f"Combined amount {combined_amount} reaches threshold {threshold} "
                     f"while each PR ({amount}) stays under it — possible threshold avoidance."),
            evidence=["anomaly_report.json", "historical_prs.csv"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {split_route} for threshold-avoidance review.",
            status=FindingStatus.OPEN,
        ))

    if split_order_detected:
        result = "split_order_detected"
    elif anomaly_detected:
        result = "anomaly_detected"
    else:
        result = "clean"

    report = AnomalyReport(
        pr_id=pr_id,
        department=department,
        cost_center=cost_center,
        item_description=item,
        amount=amount,
        requested_date=requested_date,
        same_department_same_week_count=same_dept_week_total,
        same_item_count=same_item_total,
        combined_amount=combined_amount,
        threshold=threshold,
        anomaly_detected=anomaly_detected,
        split_order_detected=split_order_detected,
        threshold_avoidance=threshold_avoidance,
        result=result,
        findings=findings,
    )

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    path = store.write_json("anomaly_report.json", report.model_dump(mode="json"))

    return AgentGResult(anomaly_report=report, anomaly_report_path=path, findings=findings)
