"""LangGraph workflow skeleton for the IPRMS A->H pipeline (Task 7).

Builds a linear StateGraph A->B->C->D->E->F->G->H->ERP whose nodes call the
deterministic agent functions. LangGraph provides only flow/state/audit
structure; it never replaces business rules, and its artifacts are identical to
the direct Python pipeline. The project still runs without LangGraph (the direct
runner remains the official entrypoint).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_a_intake_context import AgentAResult
from artifact_store import RUNS_ROOT
from graph import nodes
from graph.state import PipelineState


def build_graph():
    """Compile the linear A->H->ERP LangGraph."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(PipelineState)
    g.add_node("intake", nodes.node_intake)
    g.add_node("extract", nodes.node_extract)
    g.add_node("budget", nodes.node_budget)
    g.add_node("vendor", nodes.node_vendor)
    g.add_node("compliance", nodes.node_compliance)
    g.add_node("sole_source", nodes.node_sole_source)
    g.add_node("anomaly", nodes.node_anomaly)
    g.add_node("decision", nodes.node_decision)
    g.add_node("erp", nodes.node_erp)

    order = ["intake", "extract", "budget", "vendor", "compliance",
             "sole_source", "anomaly", "decision", "erp"]
    g.add_edge(START, order[0])
    for a, b in zip(order, order[1:]):
        g.add_edge(a, b)
    g.add_edge(order[-1], END)
    return g.compile()


def run_graph(bundle_dir: Path | str, *, run_id: Optional[str] = None,
              runs_root: Path | str = RUNS_ROOT, when: Optional[datetime] = None,
              processing_time_seconds: float = 0.0) -> AgentAResult:
    """Execute the LangGraph skeleton and return the shared AgentAResult."""
    app = build_graph()
    final_state = app.invoke({
        "bundle_dir": str(bundle_dir),
        "runs_root": str(runs_root),
        "run_id": run_id,
        "when": when,
        "processing_time_seconds": processing_time_seconds,
    })
    return final_state["ares"]
