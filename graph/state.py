"""Shared state for the IPRMS LangGraph skeleton.

Carries the bundle parameters and the shared AgentAResult (which owns the run
directory every agent writes into). LangGraph only moves this state between
nodes; all business logic lives in the deterministic agent functions.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class PipelineState(TypedDict, total=False):
    bundle_dir: str
    runs_root: str
    run_id: Optional[str]
    when: Any                     # datetime | None
    processing_time_seconds: float
    ares: Any                     # AgentAResult (set by the intake node)
