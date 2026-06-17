import shutil

import pytest

from configs.config import PR_BUNDLES_DIR
from input_reader import (
    ReaderResult,
    read_digital_pdf,
    read_input,
    read_json,
    read_web_form,
)

BUNDLE = PR_BUNDLES_DIR / "pr_bundle_001"


def test_read_json_keeps_full_fields():
    r = read_json(BUNDLE / "requisition_form.json")
    assert r.input_type == "json"
    assert r.fields["pr_id"] == "PR-2026-001"
    # whole JSON preserved — including provenance-style fields if present
    assert "simulated_confidence_score" in r.fields
    assert r.fields["simulated_confidence_score"] == 0.96


def test_read_web_form_payload_passthrough():
    payload = {"pr_id": "PR-WEB-1", "vendor_name": "Acme", "estimated_amount": 100}
    r = read_web_form(payload)
    assert r.input_type == "web_form"
    assert r.fields == payload


def test_read_digital_pdf_extracts_text_and_word_boxes():
    r = read_digital_pdf(BUNDLE / "requisition_form.pdf")
    assert r.input_type == "pdf"
    assert "Gjirafa Mall" in r.raw_text
    assert r.pages and r.pages[0].page_number == 1
    words = r.pages[0].words
    assert words, "expected word boxes from the digital PDF"
    for w in words:
        assert len(w.bbox) == 4, f"bbox must have 4 numbers: {w.bbox}"


def test_dispatcher_routes_by_input_type():
    pdf = read_input(BUNDLE / "requisition_form.pdf", "pdf")
    assert isinstance(pdf, ReaderResult) and pdf.input_type == "pdf"
    js = read_input(BUNDLE / "requisition_form.json", "json")
    assert js.input_type == "json" and js.fields["cost_center"] == "CC-IT-001"


def test_unsupported_input_type_raises():
    with pytest.raises(ValueError):
        read_input(BUNDLE / "requisition_form.json", "xml")


def test_read_digital_pdf_with_pdfplumber_engine():
    r = read_digital_pdf(BUNDLE / "requisition_form.pdf", engine="pdfplumber")
    assert r.input_type == "pdf"
    assert "Gjirafa Mall" in r.raw_text
    words = r.pages[0].words
    assert words
    for w in words:
        assert len(w.bbox) == 4


def test_unknown_pdf_engine_raises():
    with pytest.raises(ValueError):
        read_digital_pdf(BUNDLE / "requisition_form.pdf", engine="nope")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract OCR not installed")
def test_scanned_pdf_ocr_when_available():
    from input_reader import read_scanned_pdf
    r = read_scanned_pdf(BUNDLE / "requisition_form.pdf")
    assert r.ocr_used is True
    assert r.input_type == "scanned_pdf"
    assert r.raw_text.strip() != ""


def test_scanned_pdf_ocr_path_with_mock(monkeypatch):
    """Verify the OCR code path (render -> image_to_data -> WordBox) without a real
    Tesseract binary, by injecting a fake pytesseract module."""
    import sys
    import types

    import input_reader

    fake = types.SimpleNamespace(
        Output=types.SimpleNamespace(DICT="dict"),
        image_to_data=lambda img, output_type=None: {
            "text": ["Gjirafa", "Mall", "  "],
            "left": [10, 80, 0], "top": [20, 20, 0],
            "width": [60, 40, 0], "height": [15, 15, 0],
        },
        image_to_string=lambda img: "Gjirafa Mall\n",
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(input_reader, "ocr_available", lambda: True)

    r = input_reader.read_scanned_pdf(BUNDLE / "requisition_form.pdf")
    assert r.ocr_used is True
    assert r.input_type == "scanned_pdf"
    assert "Gjirafa Mall" in r.raw_text
    # blank OCR tokens are dropped; remaining words keep 4-number boxes
    words = r.pages[0].words
    assert [w.text for w in words] == ["Gjirafa", "Mall"]
    for w in words:
        assert len(w.bbox) == 4


def test_ocr_unavailable_raises():
    import input_reader
    from input_reader import OcrUnavailableError

    monkeypatch_avail = input_reader.ocr_available
    try:
        input_reader.ocr_available = lambda: False
        with pytest.raises(OcrUnavailableError):
            input_reader.read_scanned_pdf(BUNDLE / "requisition_form.pdf")
    finally:
        input_reader.ocr_available = monkeypatch_avail