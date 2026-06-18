"""FastAPI app for IPRMS (plan §11, Task 9).

Exposes endpoints that trigger the local deterministic Python pipeline
(pipelines/run_iprms_pipeline.py) and read the generated artifacts from
runs/<run_id>/. No cloud services — everything is local file-based.

Endpoints:
  POST /run-pr                  -> run a PR bundle, return run_id + decision
  GET  /runs/{run_id}           -> run context + decision summary
  GET  /runs/{run_id}/decision  -> approval_packet.json (final decision)
  GET  /runs/{run_id}/artifacts -> list of artifacts in the run dir
  GET  /runs/{run_id}/summary   -> run_summary.csv row + metrics.json
  GET  /metrics                 -> aggregate metrics across all runs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from configs.config import RUNS_DIR as _DEFAULT_RUNS_DIR
from manifest_validation import ManifestValidationError
from pipelines.run_iprms_pipeline import run_pipeline
from reporting import summarize_run, validate_run

# Module-level so tests can override where runs are written/read.
RUNS_DIR = _DEFAULT_RUNS_DIR

app = FastAPI(title="IPRMS API", version="1.0")


class RunRequest(BaseModel):
    bundle_dir: str
    use_langgraph: bool = False


def _run_dir(run_id: str) -> Path:
    d = Path(RUNS_DIR) / run_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return d


def _read_json(run_id: str, name: str) -> dict:
    p = _run_dir(run_id) / name
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/run-pr")
def run_pr(req: RunRequest) -> dict:
    try:
        res = run_pipeline(req.bundle_dir, runs_root=RUNS_DIR, use_langgraph=req.use_langgraph)
    except ManifestValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "run_id": res.run_id,
        "final_decision": res.final_decision,
        "po_status": res.po_status,
        "erp_status": res.erp_status,
        "exception_count": res.exception_count,
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    _run_dir(run_id)  # 404 if missing
    context = _read_json(run_id, "context_packet.json")
    packet = _read_json(run_id, "approval_packet.json")
    return {
        "run_id": run_id,
        "bundle_id": context.get("bundle_id"),
        "pr_type": context.get("pr_type"),
        "input_hash": context.get("input_hash"),
        "final_decision": packet.get("final_decision"),
        "po_status": packet.get("po_status"),
        "routed_to": packet.get("routed_to"),
        "exception_count": packet.get("exception_count"),
    }


@app.get("/runs/{run_id}/decision")
def get_decision(run_id: str) -> dict:
    return _read_json(run_id, "approval_packet.json")


@app.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str) -> dict:
    d = _run_dir(run_id)
    artifacts = sorted(p.name for p in d.iterdir() if p.is_file())
    return {"run_id": run_id, "artifacts": artifacts}


@app.get("/runs/{run_id}/audit-log", response_class=PlainTextResponse)
def get_audit_log(run_id: str) -> str:
    p = _run_dir(run_id) / "audit_log.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="audit_log.md not found")
    return p.read_text(encoding="utf-8")


@app.get("/runs/{run_id}/summary")
def get_summary(run_id: str) -> dict:
    _run_dir(run_id)
    return summarize_run(_run_dir(run_id))


@app.get("/metrics")
def get_metrics() -> dict:
    root = Path(RUNS_DIR)
    runs = []
    by_decision: dict = {}
    if root.is_dir():
        for d in sorted(root.iterdir()):
            mp = d / "metrics.json"
            if mp.is_file():
                m = json.loads(mp.read_text(encoding="utf-8"))
                runs.append(m)
                dec = m.get("final_decision", "unknown")
                by_decision[dec] = by_decision.get(dec, 0) + 1
    return {"total_runs": len(runs), "by_decision": by_decision, "runs": runs}
