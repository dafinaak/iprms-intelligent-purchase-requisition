import csv
import json

from artifact_store import ArtifactStore, RUN_SUMMARY_COLUMNS, RUNS_ROOT
from configs.config import REPO_ROOT, RUNS_DIR


def test_runs_root_is_repo_anchored():
    # runs/ must live at the repo root, not at the current working directory.
    assert RUNS_DIR == REPO_ROOT / "runs"
    assert RUNS_ROOT == RUNS_DIR
    assert RUNS_DIR.is_absolute()


def test_run_dir_created(tmp_path):
    store = ArtifactStore("RUN-001", root=tmp_path)
    assert store.run_dir.is_dir()
    assert store.path("budget_check.json") == store.run_dir / "budget_check.json"


def test_write_json_roundtrip(tmp_path):
    store = ArtifactStore("RUN-001", root=tmp_path)
    p = store.write_json("budget_check.json", {"result": "passed", "available": 7000})
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["result"] == "passed"
    assert store.read_json("budget_check.json")["available"] == 7000


def test_write_markdown(tmp_path):
    store = ArtifactStore("RUN-001", root=tmp_path)
    p = store.write_markdown("exceptions.md", "# Exceptions\n\nNo critical exceptions.\n")
    assert p.read_text(encoding="utf-8").startswith("# Exceptions")


def test_write_run_summary_columns(tmp_path):
    store = ArtifactStore("RUN-001", root=tmp_path)
    p = store.write_run_summary({
        "run_id": "RUN-001", "pr_id": "PR-2026-001", "final_decision": "auto_po",
        "exception_count": 0, "highest_severity": "none",
    })
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == RUN_SUMMARY_COLUMNS   # exact canonical header
        row = next(reader)
    assert row["final_decision"] == "auto_po"
    assert row["currency"] == ""   # missing keys filled blank
