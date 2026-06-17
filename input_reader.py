"""PR input reader for IPRMS.

Reads a requisition in any supported form — JSON, digital PDF, scanned PDF, or
web-form payload — and normalises it into a single ReaderResult that carries raw
text and evidence metadata (page + word bounding boxes) for Agent B.

Standalone: uses local PyMuPDF/pdfplumber parsing and a local OCR engine
(pytesseract). No cloud document-intelligence service is used.

Separation of concerns: Agent A detects the input type (from the manifest) and
passes it in; this reader only reads. The single exception is the auto-fallback
of a text-less "pdf" to scanned_pdf, which is a reading detail, not type detection.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF


@dataclass
class WordBox:
    text: str
    bbox: List[float]      # [x0, y0, x1, y1]


@dataclass
class PageText:
    page_number: int
    text: str
    words: List[WordBox] = field(default_factory=list)


@dataclass
class ReaderResult:
    input_type: str                            # json | pdf | scanned_pdf | web_form
    raw_text: str
    pages: List[PageText] = field(default_factory=list)
    fields: Optional[Dict[str, Any]] = None    # structured fields (JSON / web form)
    source_file: Optional[str] = None
    ocr_used: bool = False


class OcrUnavailableError(RuntimeError):
    """Raised when a scanned PDF needs OCR but no Tesseract binary is available."""


def ocr_available() -> bool:
    try:
        import pytesseract
    except Exception:
        return False
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
        return Path(cmd).exists()
    return shutil.which("tesseract") is not None


# ---------- structured inputs ----------
def read_json(path: Path | str) -> ReaderResult:
    # Keep the WHOLE JSON in `fields` (e.g. simulated_confidence_score) — Agent B reads it.
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ReaderResult("json", json.dumps(data, ensure_ascii=False, indent=2),
                        fields=data, source_file=str(path))


def read_web_form(payload: Dict[str, Any]) -> ReaderResult:
    return ReaderResult("web_form", json.dumps(payload, ensure_ascii=False, indent=2),
                        fields=dict(payload))


# ---------- PDF inputs ----------
def _read_pymupdf(path: Path | str) -> ReaderResult:
    doc = fitz.open(path)
    pages, full = [], []
    for i, page in enumerate(doc, start=1):
        words = [WordBox(w[4], [w[0], w[1], w[2], w[3]]) for w in page.get_text("words")]
        text = page.get_text("text")
        pages.append(PageText(i, text, words))
        full.append(text)
    doc.close()
    return ReaderResult("pdf", "\n".join(full), pages=pages, source_file=str(path))


def _read_pdfplumber(path: Path | str) -> ReaderResult:
    import pdfplumber

    pages, full = [], []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = [
                WordBox(w["text"], [float(w["x0"]), float(w["top"]),
                                    float(w["x1"]), float(w["bottom"])])
                for w in page.extract_words()
            ]
            text = page.extract_text() or ""
            pages.append(PageText(i, text, words))
            full.append(text)
    return ReaderResult("pdf", "\n".join(full), pages=pages, source_file=str(path))


def read_digital_pdf(path: Path | str, engine: str = "auto") -> ReaderResult:
    """Parse a digital PDF into text + word boxes.

    engine: "pymupdf" | "pdfplumber" | "auto" (PyMuPDF first, fall back to
    pdfplumber if PyMuPDF finds no text layer).
    """
    engine = engine.lower()
    if engine == "pymupdf":
        return _read_pymupdf(path)
    if engine == "pdfplumber":
        return _read_pdfplumber(path)
    if engine == "auto":
        result = _read_pymupdf(path)
        if not result.raw_text.strip():
            return _read_pdfplumber(path)
        return result
    raise ValueError(f"Unknown PDF engine: {engine!r}")


def read_scanned_pdf(path: Path | str, dpi: int = 200) -> ReaderResult:
    if not ocr_available():
        raise OcrUnavailableError(
            "Tesseract OCR not found. Install it and/or set TESSERACT_CMD in .env."
        )
    import pytesseract
    from PIL import Image

    doc = fitz.open(path)
    pages, full = [], []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        words = [
            WordBox(t, [float(data["left"][j]), float(data["top"][j]),
                        float(data["left"][j] + data["width"][j]),
                        float(data["top"][j] + data["height"][j])])
            for j, t in enumerate(data["text"]) if t.strip()
        ]
        text = pytesseract.image_to_string(img)
        pages.append(PageText(i, text, words))
        full.append(text)
    doc.close()
    return ReaderResult("scanned_pdf", "\n".join(full), pages=pages,
                        source_file=str(path), ocr_used=True)


# ---------- unified dispatcher ----------
def read_input(source: Any, input_type: str) -> ReaderResult:
    """Normalise any supported input into a ReaderResult for Agent B.

    `input_type` is supplied by Agent A (from the manifest); the reader does not
    detect it. The only exception is a text-less "pdf" falling back to OCR.
    """
    it = input_type.lower()
    if it == "json":
        return read_json(source)
    if it == "web_form":
        return read_web_form(source)
    if it == "scanned_pdf":
        return read_scanned_pdf(source)
    if it == "pdf":
        result = read_digital_pdf(source)
        if not result.raw_text.strip() and ocr_available():
            return read_scanned_pdf(source)
        return result
    raise ValueError(f"Unsupported input_type: {input_type!r}")