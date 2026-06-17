"""Official IPRMS pipeline runner (plan §10, Task 7).

Executes the full deterministic 8-agent sequence locally, in order:
    Agent A -> B -> C -> D -> E -> F -> G -> H  (+ ERP/tracker stub)

This direct Python path is the OFFICIAL execution flow. An optional LangGraph
skeleton (graph/workflow.py) wraps the same deterministic functions for
flow/state/audit; its output must match this direct path for the same input and
config (verified by parity tests). The project runs fully without LangGraph.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import erp_stub
from agents import agent_a_intake_context as agent_a
from agents import agent_b_item_pr_extraction as agent_b
from agents import agent_c_budget_validation as agent_c
from agents import agent_d_vendor_matching as agent_d
from agents import agent_e_policy_compliance as agent_e
from agents import agent_f_sole_source_bid_threshold as agent_f
from agents import agent_g_split_order_anomaly as agent_g
from agents import agent_h_exception_triage_orchestration as agent_h
from agents.agent_a_intake_context import AgentAResult
from artifact_store import RUNS_ROOT


@dataclass
class PipelineResult:
    run_id: str
    run_dir: Path
    final_decision: str
    po_status: str
    erp_status: str
    exception_count: int


def run_direct(bundle_dir: Path | str, *, run_id: Optional[str] = None,
               runs_root: Path | str = RUNS_ROOT, when: Optional[datetime] = None,
               processing_time_seconds: float = 0.0) -> AgentAResult:
    """Deterministic A->H (+ERP) execution. Returns the shared AgentAResult."""
    ares = agent_a.run(bundle_dir, run_id=run_id, runs_root=runs_root, when=when)
    agent_b.run(ares)
    agent_c.run(ares)
    agent_d.run(ares)
    agent_e.run(ares)
    agent_f.run(ares)
    agent_g.run(ares)
    agent_h.run(ares, processing_time_seconds=processing_time_seconds)
    erp_stub.run(ares)
    return ares


def _summarize(ares: AgentAResult) -> PipelineResult:
    packet = json.loads((ares.run_dir / "approval_packet.json").read_text(encoding="utf-8"))
    erp = json.loads((ares.run_dir / "erp_posting_result.json").read_text(encoding="utf-8"))
    return PipelineResult(
        run_id=ares.run_id,
        run_dir=ares.run_dir,
        final_decision=packet["final_decision"],
        po_status=packet["po_status"],
        erp_status=erp["erp_status"],
        exception_count=packet["exception_count"],
    )


def run_pipeline(bundle_dir: Path | str, *, run_id: Optional[str] = None,
                 runs_root: Path | str = RUNS_ROOT, when: Optional[datetime] = None,
                 use_langgraph: bool = False,
                 processing_time_seconds: float = 0.0) -> PipelineResult:
    """Run the pipeline. Direct deterministic path by default; optional LangGraph
    skeleton (use_langgraph=True) which must produce identical artifacts."""
    if use_langgraph:
        from graph.workflow import run_graph
        ares = run_graph(bundle_dir, run_id=run_id, runs_root=runs_root, when=when,
                         processing_time_seconds=processing_time_seconds)
    else:
        ares = run_direct(bundle_dir, run_id=run_id, runs_root=runs_root, when=when,
                          processing_time_seconds=processing_time_seconds)
    return _summarize(ares)


def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the IPRMS deterministic 8-agent pipeline.")
    parser.add_argument("--bundle", required=True, help="Path to the PR bundle folder.")
    parser.add_argument("--run-id", default=None, help="Optional explicit run id.")
    parser.add_argument("--langgraph", action="store_true",
                        help="Use the LangGraph internal skeleton (must match direct output).")
    args = parser.parse_args(argv)

    from manifest_validation import ManifestValidationError
    try:
        res = run_pipeline(args.bundle, run_id=args.run_id, use_langgraph=args.langgraph)
    except ManifestValidationError as e:
        print(f"[IPRMS] BLOCKED — {e}")
        return 1

    print(f"[IPRMS] run_id={res.run_id}")
    print(f"[IPRMS] run_dir={res.run_dir}")
    print(f"[IPRMS] decision={res.final_decision}  po_status={res.po_status}")
    print(f"[IPRMS] erp_status={res.erp_status}  exceptions={res.exception_count}")
    print(f"[IPRMS] engine={'LangGraph skeleton' if args.langgraph else 'direct Python'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
