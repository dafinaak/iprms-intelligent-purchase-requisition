"""Local run-artifact storage for IPRMS.

Every run writes its artifacts under runs/<run_id>/ as JSON, Markdown and CSV.
This module centralises path resolution and writing so all agents (A–H) and the
pipeline use one consistent storage layout. See plani.pdf §7 for the mandatory
artifact list.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, List, Mapping, Sequence

from configs.config import RUNS_DIR

# Single source of truth for where runs live (absolute, cwd-independent).
RUNS_ROOT = RUNS_DIR

# Mandatory artifacts every run must produce (plani.pdf §7)
MANDATORY_ARTIFACTS: List[str] = [
    "context_packet.json",
    "evidence_index.json",
    "extracted_pr.json",
    "budget_check.json",
    "vendor_match.json",
    "policy_check.json",
    "sole_source_check.json",
    "bid_threshold_check.json",
    "anomaly_report.json",
    "exceptions.md",
    "approval_packet.json",
    "po_draft.json",
    "audit_log.md",
    "metrics.json",
    "run_summary.csv",
]

# Additional mandatory artifacts (plani.pdf §7 / §12)
ADDITIONAL_ARTIFACTS: List[str] = [
    "tracker_payload.json",
    "erp_posting_result.json",
]

# run_summary.csv columns (plani.pdf §7 / §8)
RUN_SUMMARY_COLUMNS: List[str] = [
    "run_id",
    "pr_id",
    "requester",
    "cost_center",
    "vendor_name",
    "requested_amount",
    "currency",
    "final_decision",
    "exception_count",
    "highest_severity",
    "routed_to",
    "po_status",
    "processing_time_seconds",
    "idempotency_check",
]


class ArtifactStore:
    """Creates runs/<run_id>/ and writes artifacts into it."""

    def __init__(self, run_id: str, root: Path | str = RUNS_ROOT) -> None:
        self.run_id = run_id
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        """Absolute path of an artifact inside this run directory."""
        return self.run_dir / name

    def write_json(self, name: str, data: Any, *, indent: int = 2) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
        return p

    def write_markdown(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.write_text(text, encoding="utf-8")
        return p

    def write_csv(self, name: str, rows: Sequence[Mapping[str, Any]],
                  header: Sequence[str] | None = None) -> Path:
        p = self.path(name)
        fieldnames = list(header) if header else (list(rows[0].keys()) if rows else [])
        with p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        return p

    def write_run_summary(self, row: Mapping[str, Any]) -> Path:
        """Write the single-row run_summary.csv using the canonical columns."""
        return self.write_csv("run_summary.csv", [row], header=RUN_SUMMARY_COLUMNS)

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text(encoding="utf-8"))
