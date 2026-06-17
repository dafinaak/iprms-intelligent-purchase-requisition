"""Agent C - Budget Validation (IPRMS).

Validates whether the requested PR amount fits the available cost-center budget
(plani.pdf §6). Reads extracted_pr.json (Agent B) and the bundle's
budget_snapshot.csv + cost_center_mapping.csv (paths resolved from Agent A's
evidence_index). Produces budget_check.json and findings (Unified Findings Schema).

Deterministic & rule-based — no LLM is involved in budget decisions.
Possible results: passed | failed_budget_exhausted | route_to_FP&A.
"""
from __future__ import annotations

import csv
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
from schemas.finding_schema import Finding, FindingStatus, Severity
from schemas.pr_schema import BudgetCheck, BudgetResult

SOURCE_AGENT = "Agent C"


@dataclass
class AgentCResult:
    budget_check: BudgetCheck
    budget_check_path: Path
    findings: List[Finding] = field(default_factory=list)


def _field_value(data: Dict[str, Any], key: str) -> Any:
    """Read a value from extracted_pr.json (per-field {value,...} or scalar)."""
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


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None) -> AgentCResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))

    pr_id = _field_value(extracted, "pr_id") or ""
    cost_center = _field_value(extracted, "cost_center") or ""
    requested = _to_float(_field_value(extracted, "estimated_amount"))
    currency = _field_value(extracted, "currency") or ""

    # Routing target for budget problems (configurable via policy_pack).
    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    fpa_route = policy.get("routing", {}).get("budget_exhausted", "FP&A")

    # Load budget snapshot + cost-center mapping.
    budget_rows = list(csv.DictReader(_evidence_path(ares, "budget_snapshot").open(encoding="utf-8")))
    mapping_rows = list(csv.DictReader(_evidence_path(ares, "cost_center_mapping").open(encoding="utf-8")))

    budget_row = next((r for r in budget_rows if r.get("cost_center") == cost_center), None)
    in_mapping = any(r.get("cost_center") == cost_center for r in mapping_rows)
    cost_center_exists = budget_row is not None and in_mapping

    findings: List[Finding] = []
    available = _to_float(budget_row.get("available_budget")) if budget_row else 0.0

    if not cost_center_exists:
        result = BudgetResult.ROUTE_TO_FPA
        routed_to = fpa_route
        findings.append(Finding(
            finding_id="F-C-001",
            finding_type="COST_CENTER_NOT_FOUND",
            severity=Severity.HIGH,
            confidence=1.0,
            message=f"Cost center '{cost_center}' not found in budget snapshot / mapping.",
            evidence=["budget_check.json", "evidence_index.json"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {fpa_route}.",
            status=FindingStatus.OPEN,
        ))
    elif available < requested:
        result = BudgetResult.FAILED_BUDGET_EXHAUSTED
        routed_to = fpa_route
        findings.append(Finding(
            finding_id="F-C-001",
            finding_type="BUDGET_EXCEEDED",
            severity=Severity.HIGH,
            confidence=0.99,
            message=(f"Budget exhausted for {cost_center}: available {available} "
                     f"< requested {requested} {currency}."),
            evidence=["budget_check.json"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Block PO and route to {fpa_route}.",
            status=FindingStatus.OPEN,
        ))
    else:
        result = BudgetResult.PASSED
        routed_to = None

    budget_check = BudgetCheck(
        pr_id=pr_id,
        cost_center=cost_center,
        cost_center_exists=cost_center_exists,
        requested_amount=requested,
        available_budget=available,
        currency=currency,
        result=result,
        routed_to=routed_to,
        findings=findings,
    )

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    path = store.write_json("budget_check.json", budget_check.model_dump(mode="json"))

    return AgentCResult(budget_check=budget_check, budget_check_path=path, findings=findings)
