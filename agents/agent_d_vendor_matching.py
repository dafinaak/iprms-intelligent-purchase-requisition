"""Agent D - Vendor Matching (IPRMS).

Matches the requested vendor against approved_vendors.csv and classifies it as
approved/preferred / non-preferred / not-approved (plani.pdf §6). Produces
vendor_match.json and findings (Unified Findings Schema).

Scope note: catalogue pricing comparison and vague-item detection are added by
Task 17; here the price fields are left None. Deterministic & rule-based — no LLM
decides vendor approval.
"""
from __future__ import annotations

import csv
import difflib
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
from schemas.pr_schema import VendorMatch, VendorMatchResult

SOURCE_AGENT = "Agent D"
MATCH_THRESHOLD = 0.85


@dataclass
class AgentDResult:
    vendor_match: VendorMatch
    vendor_match_path: Path
    match_score: float = 0.0
    findings: List[Finding] = field(default_factory=list)


def _field_value(data: Dict[str, Any], key: str) -> Any:
    f = data.get(key)
    return f.get("value") if isinstance(f, dict) else f


def _evidence_path(ares: AgentAResult, role: str) -> Path:
    for ev in ares.evidence_index.get("evidence", []):
        if ev.get("role") == role:
            return Path(ev["path"])
    raise KeyError(f"evidence role not found: {role}")


def _normalize(name: Any) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _best_match(req_norm: str, rows: List[Dict[str, str]]):
    best_row, best_score = None, 0.0
    for r in rows:
        score = difflib.SequenceMatcher(None, req_norm, _normalize(r.get("vendor_name"))).ratio()
        if score > best_score:
            best_row, best_score = r, score
    return best_row, best_score


def run(ares: AgentAResult, *, policy: Optional[Dict[str, Any]] = None) -> AgentDResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))

    pr_id = _field_value(extracted, "pr_id") or ""
    vendor_name = _field_value(extracted, "vendor_name") or ""
    justification = str(_field_value(extracted, "business_justification") or "").strip()

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    proc_route = policy.get("routing", {}).get("non_preferred_vendor", "Procurement")

    rows = list(csv.DictReader(_evidence_path(ares, "approved_vendors").open(encoding="utf-8")))
    row, score = _best_match(_normalize(vendor_name), rows)

    if row is not None and score >= MATCH_THRESHOLD:
        approved = row.get("approved_status") == "approved"
        preferred = row.get("preferred_status") == "preferred"
        vendor_status = row.get("approved_status") or "unknown"
    else:
        approved, preferred, vendor_status, score = False, False, "not_found", score

    justification_present = bool(justification)
    findings: List[Finding] = []

    if not approved:
        result = VendorMatchResult.NOT_APPROVED
        findings.append(Finding(
            finding_id="F-D-001",
            finding_type="VENDOR_NOT_APPROVED",
            severity=Severity.HIGH,
            confidence=0.95,
            message=f"Vendor '{vendor_name}' is not on the approved vendor list (status={vendor_status}).",
            evidence=["vendor_match.json"],
            source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {proc_route}.",
            status=FindingStatus.OPEN,
        ))
    elif preferred:
        result = VendorMatchResult.MATCHED
    else:
        result = VendorMatchResult.NON_PREFERRED
        if not justification_present:
            findings.append(Finding(
                finding_id="F-D-001",
                finding_type="NON_PREFERRED_VENDOR",
                severity=Severity.HIGH,
                confidence=0.95,
                message=f"Non-preferred vendor '{vendor_name}' selected without written justification.",
                evidence=["vendor_match.json"],
                source_agent=SOURCE_AGENT,
                recommended_action=f"Route to {proc_route}.",
                status=FindingStatus.OPEN,
            ))
        else:
            findings.append(Finding(
                finding_id="F-D-001",
                finding_type="NON_PREFERRED_VENDOR",
                severity=Severity.LOW,
                confidence=0.9,
                message=f"Non-preferred vendor '{vendor_name}' selected with written justification.",
                evidence=["vendor_match.json"],
                source_agent=SOURCE_AGENT,
                recommended_action="Document justification; no exception required.",
                status=FindingStatus.OPEN,
            ))

    vendor_match = VendorMatch(
        pr_id=pr_id,
        vendor_name=vendor_name,
        approved=approved,
        preferred=preferred,
        vendor_status=vendor_status,
        justification_present=justification_present,
        result=result,
        findings=findings,
    )

    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    path = store.write_json("vendor_match.json", vendor_match.model_dump(mode="json"))

    return AgentDResult(
        vendor_match=vendor_match,
        vendor_match_path=path,
        match_score=round(score, 4),
        findings=findings,
    )
