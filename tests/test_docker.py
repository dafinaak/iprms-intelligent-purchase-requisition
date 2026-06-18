"""Sanity checks for the Docker demo setup (Task 10).

Does not build the image (heavy); verifies the Dockerfile/.dockerignore are
well-formed and reference the right entrypoints.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_present_and_well_formed():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert text.startswith("#") or text.startswith("FROM")
    assert "FROM python:3.12-slim" in text
    assert "COPY requirements.txt" in text
    assert "pip install --no-cache-dir -r requirements.txt" in text
    assert "EXPOSE 8000 8501" in text
    assert "uvicorn" in text and "api.main:app" in text


def test_dockerignore_excludes_heavy_dirs():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "__pycache__/", ".git/", "runs/*"):
        assert pattern in text


def test_readme_documents_docker_commands():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docker build -t iprms ." in text
    assert "docker run" in text
    assert "streamlit run app/streamlit_app.py" in text
