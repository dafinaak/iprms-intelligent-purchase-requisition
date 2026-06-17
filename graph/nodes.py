"""LangGraph nodes for the IPRMS A->H pipeline skeleton.

Each node wraps a DETERMINISTIC agent function — no business logic lives here.
The intake node creates the shared AgentAResult; every later node runs its agent
against that same run directory. Output is therefore identical to the direct
Python pipeline (parity).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

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
from graph.state import PipelineState


def node_intake(state: PipelineState) -> Dict[str, Any]:
    ares = agent_a.run(
        state["bundle_dir"],
        run_id=state.get("run_id"),
        runs_root=state["runs_root"],
        when=state.get("when"),
    )
    return {"ares": ares}


def node_extract(state: PipelineState) -> Dict[str, Any]:
    agent_b.run(state["ares"])
    return {}


def node_budget(state: PipelineState) -> Dict[str, Any]:
    agent_c.run(state["ares"])
    return {}


def node_vendor(state: PipelineState) -> Dict[str, Any]:
    agent_d.run(state["ares"])
    return {}


def node_compliance(state: PipelineState) -> Dict[str, Any]:
    agent_e.run(state["ares"])
    return {}


def node_sole_source(state: PipelineState) -> Dict[str, Any]:
    agent_f.run(state["ares"])
    return {}


def node_anomaly(state: PipelineState) -> Dict[str, Any]:
    agent_g.run(state["ares"])
    return {}


def node_decision(state: PipelineState) -> Dict[str, Any]:
    agent_h.run(state["ares"], processing_time_seconds=state.get("processing_time_seconds", 0.0))
    return {}


def node_erp(state: PipelineState) -> Dict[str, Any]:
    erp_stub.run(state["ares"])
    return {}
