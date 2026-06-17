"""ERP API stub + Jira/ADO tracker payload (IPRMS, plan §12).

Simulates posting the generated PO draft to an ERP/procurement system and, when
the PR has an exception, creates a tracker payload routed to the right team.

ERP rule:
  * clean PR (po_status == ready_for_posting) -> erp_status = simulated_post_success
  * blocked PR (exceptions)                    -> erp_status = not_posted

Tracker rule: only created when an exception exists (decision != auto_po/auto_approve),
routed to FP&A / Procurement / Compliance / Manual Review (from approval_packet).

No business decisions are made here — this only reflects Agent H's outcome.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import ArtifactStore
from schemas.decision_schema import ErpPostingResult, TrackerPayload

CLEAN_DECISIONS = {"auto_po", "auto_approve"}


@dataclass
class ErpResult:
    erp_posting_result: ErpPostingResult
    erp_posting_result_path: Path
    tracker_payload: Optional[TrackerPayload] = None
    tracker_payload_path: Optional[Path] = None


def post_po(po_draft: Dict[str, Any]) -> ErpPostingResult:
    """Simulate POST /erp/po with po_draft.json as input."""
    run_id = po_draft.get("run_id", "")
    pr_id = po_draft.get("pr_id", "")
    po_status = po_draft.get("po_status", "blocked")
    success = po_status == "ready_for_posting"
    return ErpPostingResult(
        run_id=run_id,
        pr_id=pr_id,
        po_status=po_status,
        erp_status="simulated_post_success" if success else "not_posted",
        posted=success,
        po_number=f"PO-{run_id}" if success else None,
        message=("PO posted to ERP (simulated)." if success
                 else "PO not posted: blocked by exception(s)."),
    )


def build_tracker_payload(approval_packet: Dict[str, Any]) -> Optional[TrackerPayload]:
    """Create a tracker task only when an exception exists."""
    decision = approval_packet.get("final_decision", "")
    if decision in CLEAN_DECISIONS:
        return None  # clean PR -> no tracker task

    routed_to = approval_packet.get("routed_to") or "Procurement"
    pr_id = approval_packet.get("pr_id", "")
    findings = [
        f"[{f.get('severity', '')}] {f.get('finding_type', '')}: {f.get('message', '')}"
        for f in approval_packet.get("findings", [])
    ]
    return TrackerPayload(
        run_id=approval_packet.get("run_id", ""),
        pr_id=pr_id,
        system="jira_stub",
        routed_to=routed_to,
        task_title=f"[{decision}] PR {pr_id} requires {routed_to}",
        task_description=approval_packet.get("summary", ""),
        severity=approval_packet.get("highest_severity", "none"),
        decision=decision,
        findings=findings,
    )


def run(ares: AgentAResult) -> ErpResult:
    """Read po_draft.json + approval_packet.json from the run dir; write ERP + tracker."""
    run_dir = ares.run_dir
    po_draft = json.loads((run_dir / "po_draft.json").read_text(encoding="utf-8"))
    approval_packet = json.loads((run_dir / "approval_packet.json").read_text(encoding="utf-8"))

    erp = post_po(po_draft)
    store = ArtifactStore(ares.run_id, root=run_dir.parent)
    erp_path = store.write_json("erp_posting_result.json", erp.model_dump(mode="json"))

    tracker = build_tracker_payload(approval_packet)
    tracker_path: Optional[Path] = None
    if tracker is not None:
        tracker_path = store.write_json("tracker_payload.json", tracker.model_dump(mode="json"))

    return ErpResult(
        erp_posting_result=erp,
        erp_posting_result_path=erp_path,
        tracker_payload=tracker,
        tracker_payload_path=tracker_path,
    )
