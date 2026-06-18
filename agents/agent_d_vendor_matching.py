from __future__ import annotations

import csv
import difflib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_b_item_pr_extraction import _is_vague
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


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _best_match(req_norm: str, rows: List[Dict[str, str]]):
    best_row, best_score = None, 0.0
    for r in rows:
        score = difflib.SequenceMatcher(None, req_norm, _normalize(r.get("vendor_name"))).ratio()
        if score > best_score:
            best_row, best_score = r, score
    return best_row, best_score


def _catalogue_price(rows: List[Dict[str, str]], item_desc_norm: str) -> Tuple[Optional[float], Optional[str]]:
    for r in rows:
        if _normalize(r.get("item_description")) == item_desc_norm:
            return _to_float(r.get("standard_unit_price")), r.get("currency")
    return None, None


def run(
    ares: AgentAResult,
    *,
    policy: Optional[Dict[str, Any]] = None,
    llm_fallback: Optional[Callable[[str], str]] = None,  # optional; off by default
) -> AgentDResult:
    run_dir = ares.run_dir
    extracted = json.loads((run_dir / "extracted_pr.json").read_text(encoding="utf-8"))

    pr_id = _field_value(extracted, "pr_id") or ""
    vendor_name = _field_value(extracted, "vendor_name") or ""
    justification = str(_field_value(extracted, "business_justification") or "").strip()
    item_desc = str(_field_value(extracted, "item_description") or "").strip()
    requested_unit_price = _to_float(_field_value(extracted, "unit_price"))

    if policy is None:
        import yaml
        policy = yaml.safe_load(Path(POLICY_PACK).read_text(encoding="utf-8")) or {}
    proc_route = policy.get("routing", {}).get("non_preferred_vendor", "Procurement")
    tolerance_pct = float(policy.get("tolerances", {}).get("catalogue_price_variance_percent", 5))

    findings: List[Finding] = []

    # ---- Vendor classification (Task 16) ----
    rows = list(csv.DictReader(_evidence_path(ares, "approved_vendors").open(encoding="utf-8")))
    row, score = _best_match(_normalize(vendor_name), rows)
    if row is not None and score >= MATCH_THRESHOLD:
        approved = row.get("approved_status") == "approved"
        preferred = row.get("preferred_status") == "preferred"
        vendor_status = row.get("approved_status") or "unknown"
    else:
        approved, preferred, vendor_status, score = False, False, "not_found", score

    justification_present = bool(justification)

    if not approved:
        vendor_result = VendorMatchResult.NOT_APPROVED
        findings.append(Finding(
            finding_id=f"F-D-{len(findings) + 1:03d}",
            finding_type="VENDOR_NOT_APPROVED",
            severity=Severity.HIGH, confidence=0.95,
            message=f"Vendor '{vendor_name}' is not on the approved vendor list (status={vendor_status}).",
            evidence=["vendor_match.json"], source_agent=SOURCE_AGENT,
            recommended_action=f"Route to {proc_route}.", status=FindingStatus.OPEN,
        ))
    elif preferred:
        vendor_result = VendorMatchResult.MATCHED
    else:
        vendor_result = VendorMatchResult.NON_PREFERRED
        if not justification_present:
            findings.append(Finding(
                finding_id=f"F-D-{len(findings) + 1:03d}",
                finding_type="NON_PREFERRED_VENDOR",
                severity=Severity.HIGH, confidence=0.95,
                message=f"Non-preferred vendor '{vendor_name}' selected without written justification.",
                evidence=["vendor_match.json"], source_agent=SOURCE_AGENT,
                recommended_action=f"Route to {proc_route}.", status=FindingStatus.OPEN,
            ))
        else:
            findings.append(Finding(
                finding_id=f"F-D-{len(findings) + 1:03d}",
                finding_type="NON_PREFERRED_VENDOR",
                severity=Severity.LOW, confidence=0.9,
                message=f"Non-preferred vendor '{vendor_name}' selected with written justification.",
                evidence=["vendor_match.json"], source_agent=SOURCE_AGENT,
                recommended_action="Document justification; no exception required.",
                status=FindingStatus.OPEN,
            ))

    # ---- Catalogue pricing comparison + vague detection (Task 17) ----
    catalogue_rows = list(csv.DictReader(_evidence_path(ares, "catalogue_pricing").open(encoding="utf-8")))
    catalogue_unit_price, _cat_currency = _catalogue_price(catalogue_rows, _normalize(item_desc))

    vague = _is_vague(item_desc)
    price_variance_percent: Optional[float] = None
    within_price_tolerance: Optional[bool] = None
    if catalogue_unit_price and catalogue_unit_price > 0 and requested_unit_price is not None:
        price_variance_percent = round(
            (requested_unit_price - catalogue_unit_price) / catalogue_unit_price * 100, 4
        )
        within_price_tolerance = abs(price_variance_percent) <= tolerance_pct

    if vague:
        findings.append(Finding(
            finding_id=f"F-D-{len(findings) + 1:03d}",
            finding_type="PRICING_UNCERTAIN",
            severity=Severity.MEDIUM, confidence=0.9,
            message=f"Item description '{item_desc}' is too vague to validate catalogue pricing.",
            evidence=["vendor_match.json"], source_agent=SOURCE_AGENT,
            recommended_action="Request buyer clarification.", status=FindingStatus.OPEN,
        ))
    elif within_price_tolerance is False:
        findings.append(Finding(
            finding_id=f"F-D-{len(findings) + 1:03d}",
            finding_type="PRICE_VARIANCE",
            severity=Severity.MEDIUM, confidence=0.95,
            message=(f"Requested unit price {requested_unit_price} deviates {price_variance_percent}% "
                     f"from catalogue {catalogue_unit_price} (tolerance {tolerance_pct}%)."),
            evidence=["vendor_match.json"], source_agent=SOURCE_AGENT,
            recommended_action="Review pricing before approval.", status=FindingStatus.OPEN,
        ))

    # ---- Final result ----
    if vendor_result == VendorMatchResult.NOT_APPROVED:
        result = VendorMatchResult.NOT_APPROVED
    elif vague:
        result = VendorMatchResult.PRICING_UNCERTAIN
    else:
        result = vendor_result

    vendor_match = VendorMatch(
        pr_id=pr_id,
        vendor_name=vendor_name,
        approved=approved,
        preferred=preferred,
        vendor_status=vendor_status,
        justification_present=justification_present,
        requested_unit_price=requested_unit_price,
        catalogue_unit_price=catalogue_unit_price,
        price_variance_percent=price_variance_percent,
        within_price_tolerance=within_price_tolerance,
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
