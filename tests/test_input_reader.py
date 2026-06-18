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


def test_scanned_pdf_with_injected_ocr_engine():
    """OCR via an injected engine — deterministic, no system Tesseract binary."""
    from input_reader import WordBox, read_scanned_pdf

    def fake_engine(image):
        return "Gjirafa Mall\n", [WordBox("Gjirafa", [10.0, 20.0, 70.0, 35.0]),
                                  WordBox("Mall", [80.0, 20.0, 120.0, 35.0])]

    r = read_scanned_pdf(BUNDLE / "requisition_form.pdf", ocr_engine=fake_engine)
    assert r.ocr_used is True
    assert r.input_type == "scanned_pdf"
    assert "Gjirafa Mall" in r.raw_text
    words = r.pages[0].words
    assert [w.text for w in words] == ["Gjirafa", "Mall"]
    for w in words:
        assert len(w.bbox) == 4


def test_read_input_uses_injected_ocr_for_scanned():
    from input_reader import WordBox, read_input

    engine = lambda image: ("scanned text", [WordBox("scanned", [0.0, 0.0, 10.0, 10.0])])
    r = read_input(BUNDLE / "requisition_form.pdf", "scanned_pdf", ocr_engine=engine)
    assert r.input_type == "scanned_pdf" and "scanned text" in r.raw_text


def test_ocr_unavailable_raises_without_engine(monkeypatch):
    import input_reader
    from input_reader import OcrUnavailableError

    # No engine injected AND no system OCR available -> clear error.
    monkeypatch.setattr(input_reader, "ocr_available", lambda: False)
    with pytest.raises(OcrUnavailableError):
        input_reader.read_scanned_pdf(BUNDLE / "requisition_form.pdf")