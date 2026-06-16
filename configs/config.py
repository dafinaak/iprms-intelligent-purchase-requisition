"""Central path configuration for IPRMS.

Single source of truth for where repo-relative data and run artifacts live.
All paths are resolved from this file's location, so they are correct no matter
what the current working directory is when a command is launched.
"""
from __future__ import annotations

from pathlib import Path

# configs/config.py -> parents[1] is the repository root
REPO_ROOT = Path(__file__).resolve().parents[1]

# Inputs
DATA_DIR = REPO_ROOT / "data"
PR_BUNDLES_DIR = DATA_DIR / "pr_bundles"
CONFIGS_DIR = REPO_ROOT / "configs"
POLICY_PACK = CONFIGS_DIR / "policy_pack.yaml"

# Outputs
RUNS_DIR = REPO_ROOT / "runs"
