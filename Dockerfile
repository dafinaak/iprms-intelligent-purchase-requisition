# IPRMS — local/containerized demo image (plan §10/§11).
# Docker is OPTIONAL: the system also runs directly with Python commands.
# Azure Container Apps / cloud deployment is NOT used.
FROM python:3.12-slim

# Tesseract is only needed for scanned-PDF OCR (optional); digital-PDF parsing
# (PyMuPDF / pdfplumber) needs no system dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project.
COPY . .

# FastAPI (8000) and Streamlit (8501).
EXPOSE 8000 8501

# Default: FastAPI. Override the command to run Streamlit instead (see README).
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
