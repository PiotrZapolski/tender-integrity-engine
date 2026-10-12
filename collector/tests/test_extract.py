import pytest

from tie.openings.extract import UnsupportedDocument, extract_text


def read(fixtures, name):
    return (fixtures / "openings" / name).read_bytes()


def test_text_pdf(fixtures):
    e = extract_text(read(fixtures, "kolbudy_text.pdf"), "kolbudy_text.pdf")
    assert e.method == "pdftext"
    assert "5932335316" in e.text and "213 192,00" in e.text


def test_docx(fixtures):
    e = extract_text(read(fixtures, "rogow_docx.docx"), "x.docx")
    assert e.method == "docx"
    assert "366 120,00" in e.text and "770 901,55" in e.text


def test_scan_uses_ocr(fixtures):
    e = extract_text(read(fixtures, "kielce_scan.pdf"), "scan.pdf")
    assert e.method == "ocr"
    assert len(e.text.strip()) > 200
    assert "ofert" in e.text.lower(), e.text[:500]


def test_unsupported():
    with pytest.raises(UnsupportedDocument):
        extract_text(b"x", "a.xlsx")
