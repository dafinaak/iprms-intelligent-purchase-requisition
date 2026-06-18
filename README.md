# IPRMS — Intelligent Purchase Requisition Management System

IPRMS is a standalone Python-based **8-agent purchase requisition management system**. It
processes Purchase Requisition (PR) bundles, validates budget and vendor data, applies a
configurable policy pack, detects procurement risks and anomalies, routes exceptions, and
generates ERP-ready PO drafts with full JSON / Markdown / CSV audit artifacts.

> **Scope:** Purchase Requisition handling only. Invoice handling, invoice-to-PO matching, and
> GRN matching are explicitly **out of scope**.

## Technology decisions

- The official execution flow is a **deterministic Python pipeline** (`pipelines/run_iprms_pipeline.py`), run locally or in a standard container. **No cloud platform is required** for the core implementation.
- Core business decisions are **deterministic** (Python / Pydantic / Pandas) to preserve idempotency. PySpark is optional, only for larger-scale local data processing.
- **LangGraph** is optional, only as a lightweight internal wrapper for the Agent A→H sequence (it must not replace the deterministic Python pipeline runner).
- **LangChain / LLM** is optional, only as an Agent B fallback for unclear extraction fields or vague item descriptions. It must **not** decide approval, blocking, routing, or PO posting.
- The system must work even if LangGraph / LangChain are disabled.

## The 8 agents

| Agent | Responsibility | Key output(s) |
|-------|----------------|---------------|
| A | Intake & Context / Gatekeeper | `context_packet.json`, `evidence_index.json` |
| B | Item / PR Extraction | `extracted_pr.json` |
| C | Budget Validation | `budget_check.json` |
| D | Vendor Matching | `vendor_match.json` |
| E | Compliance & Policy | `policy_check.json` |
| F | Sole-source / Bid-threshold | `sole_source_check.json`, `bid_threshold_check.json` |
| G | Split-order / Anomaly Detection | `anomaly_report.json` |
| H | Exception Triage & Lead Orchestration | `exceptions.md`, `approval_packet.json`, `po_draft.json`, `audit_log.md`, `metrics.json`, `run_summary.csv` |

## Repository structure

```
iprms-intelligent-purchase-requisition/
├── agents/        # Agent A–H implementations
├── configs/       # policy_pack.yaml, tolerance_settings.json, routing_rules.json
├── data/          # pr_bundles/ (with manifest.yaml) and sample_prs/
├── notebooks/     # local run/demo scripts (01_run_pipeline.py, 02_demo_dashboard.py)
├── pipelines/     # run_iprms_pipeline.py — deterministic A→H pipeline
├── graph/         # optional LangGraph wrapper (state.py, workflow.py)
├── llm/           # optional Agent B LangChain/LLM fallback (prompts.py, extraction_fallback.py)
├── schemas/       # Pydantic schemas (pr, artifact, finding, decision)
├── runs/          # per-run output: runs/<run_id>/ artifacts
├── tests/         # pytest scenario tests
├── api/           # FastAPI app (main.py)
├── app/           # Streamlit demo app
├── Dockerfile     # optional, for a local/containerized demo
└── requirements.txt
```

## Getting started (local setup)

> Requires Python 3.11+ (verified on 3.14). No cloud account needed.

```bash
# 1. Create & activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) configure local environment
#    Only needed for the optional LLM fallback or scanned-PDF OCR.
cp .env.example .env        # Windows: copy .env.example .env

# 4. Verify the install
python -c "import pandas, pydantic, yaml, fitz, fastapi, streamlit, pytest; print('IPRMS deps OK')"

# 5. Run the pipeline on a sample PR bundle
python pipelines/run_iprms_pipeline.py --bundle data/pr_bundles/pr_bundle_001

# 6. Run the tests
pytest tests/

# 7. Open the demo UI / API locally
streamlit run app/streamlit_app.py
uvicorn api.main:app --reload
```

**Optional system dependency:** scanned-PDF OCR uses [Tesseract](https://github.com/tesseract-ocr/tesseract).
Install it separately and, if it is not on your `PATH`, set `TESSERACT_CMD` in `.env`.
Digital-PDF parsing (PyMuPDF / pdfplumber) needs no system dependency.

## Run artifacts

Every run stores all its outputs locally under `runs/<run_id>/` as JSON, Markdown and CSV,
available for review, audit analysis, and demo reporting. The run directory is resolved from
the repo root (see `configs/config.py` → `RUNS_DIR`), so it is always created in the same place
regardless of the current working directory.

```
runs/<run_id>/
├── context_packet.json        # Agent A
├── evidence_index.json        # Agent A
├── extracted_pr.json          # Agent B
├── budget_check.json          # Agent C
├── vendor_match.json          # Agent D
├── policy_check.json          # Agent E
├── sole_source_check.json     # Agent F
├── bid_threshold_check.json   # Agent F
├── anomaly_report.json        # Agent G
├── exceptions.md              # Agent H
├── approval_packet.json       # Agent H
├── po_draft.json              # Agent H
├── audit_log.md               # Agent H
├── metrics.json               # Agent H
├── run_summary.csv            # Agent H
├── tracker_payload.json       # tracker stub (if exceptions exist)
└── erp_posting_result.json    # ERP API stub
```

Artifacts are written through `artifact_store.ArtifactStore`, which centralises path resolution
and writing (`write_json` / `write_markdown` / `write_csv` / `write_run_summary`) so all agents
use one consistent layout.

## Deployment

Docker is **optional**, only for a local/containerized demo. The system must also run directly
with plain Python commands:

```
GitHub repo → Local Python environment or Docker container
            → FastAPI / Streamlit UI → deterministic Python pipeline
            → run artifacts + JSON / Markdown / CSV outputs
```

```bash
# Build the local image
docker build -t iprms .

# Run the FastAPI service (default CMD) on http://localhost:8000
docker run --rm -p 8000:8000 iprms

# Run the Streamlit demo UI instead, on http://localhost:8501
docker run --rm -p 8501:8501 iprms \
    streamlit run app/streamlit_app.py --server.port 8501 --server.address 0.0.0.0
```

> Azure Container Apps / cloud deployment is **not** used. Docker is only for a local/containerized
> demo; the same app runs directly with `uvicorn api.main:app` / `streamlit run app/streamlit_app.py`.

## Team

| Engineer | Role |
|----------|------|
| Dafina | Platform & Data Integration Engineer |
| Yllka | PR Extraction & Matching Engineer |
| Rozafa | Compliance, Risk & Orchestration Engineer |
