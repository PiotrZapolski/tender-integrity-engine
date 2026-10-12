import io
import subprocess
import zipfile
from pathlib import Path

import pytest
from docx import Document

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


def test_ocr_renders_at_most_max_pages(monkeypatch):
    from tie.openings import extract

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    assert extract.OCR_MAX_PAGES == 20
    assert extract._ocr(b"%PDF-1.4") == ""
    pdftoppm = calls[0]
    assert pdftoppm[0] == "pdftoppm"
    assert pdftoppm[pdftoppm.index("-l") + 1] == "20"


def test_word97_doc_via_catdoc(fixtures):
    # Synthetic Word 97-2004 file (OLE2), the format of legacy ".doc" opening documents.
    e = extract_text(read(fixtures, "synthetic_word97.doc"), "Informacja z otwarcia ofert.doc")
    assert e.method == "doc"
    assert "Część nr 1" in e.text
    assert "593-233-53-16" in e.text and "213 192,00" in e.text and "250 000,00" in e.text


def test_doc_extension_with_docx_content():
    doc = Document()
    doc.add_paragraph("Alfa Sp. z o.o. 213 192,00 zł")
    buf = io.BytesIO()
    doc.save(buf)
    e = extract_text(buf.getvalue(), "otwarcie.doc")
    assert e.method == "docx" and "213 192,00" in e.text


def test_doc_that_is_not_word_raises():
    # catdoc prints any non-Word file as plain text; only OLE2 files may reach it.
    with pytest.raises(UnsupportedDocument):
        extract_text(b"not a word file at all", "x.doc")


def odt_bytes(body: str) -> bytes:
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content'
        ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        f"<office:body><office:text>{body}</office:text></office:body>"
        "</office:document-content>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
    return buf.getvalue()


def test_odt_paragraphs_and_table_rows():
    data = odt_bytes(
        "<text:h>INFORMACJA Z OTWARCIA OFERT</text:h>"
        "<text:p>Część<text:s/>nr 2</text:p>"
        "<table:table><table:table-row>"
        "<table:table-cell><text:p>Alfa Sp. z o.o.</text:p><text:p>NIP 593-233-53-16</text:p>"
        "</table:table-cell>"
        "<table:table-cell><text:p><text:span>213 192,00</text:span> zł</text:p>"
        "</table:table-cell>"
        "</table:table-row></table:table>"
    )
    e = extract_text(data, "otwarcie.ODT")
    assert e.method == "odt"
    lines = e.text.splitlines()
    assert lines[0] == "INFORMACJA Z OTWARCIA OFERT"
    assert lines[1] == "Część nr 2"
    assert lines[2] == "Alfa Sp. z o.o. NIP 593-233-53-16 | 213 192,00 zł"


def test_ocr_rotated_page_uses_orientation_detection(monkeypatch):
    # Landscape scans OCR'd with --psm 6 give garbage; tesseract's orientation detection
    # (--psm 0) finds the rotation and --psm 1 reads the page turned upright.
    from tie.openings import extract

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "pdftoppm":
            prefix = Path(cmd[-1])
            for n in (1, 2):
                prefix.with_name(f"page-{n}.png").write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        page = Path(cmd[1]).name
        if cmd[cmd.index("--psm") + 1] == "0":
            rotate = b"90" if page == "page-1.png" else b"0"
            out = (b"Page number: 0\nOrientation in degrees: 270\nRotate: " + rotate
                   + b"\nOrientation confidence: 10.80\nScript: Latin\n")
            return subprocess.CompletedProcess(cmd, 0, out, b"")
        return subprocess.CompletedProcess(cmd, 0, page.encode(), b"")

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    assert extract._ocr(b"%PDF-1.4") == "page-1.png\n\f\npage-2.png"
    ocr = [c for c in calls if c[0] == "tesseract" and "-l" in c]
    assert [Path(c[1]).name for c in ocr] == ["page-1.png", "page-2.png"]
    assert [c[c.index("--psm") + 1] for c in ocr] == ["1", "6"]
    osd = [c for c in calls if c[0] == "tesseract" and "-l" not in c]
    assert len(osd) == 2 and all(c[c.index("--psm") + 1] == "0" for c in osd)


def test_ocr_orientation_failure_keeps_default_mode(monkeypatch):
    from tie.openings import extract

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "pdftoppm":
            Path(cmd[-1]).with_name("page-1.png").write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if cmd[cmd.index("--psm") + 1] == "0":
            return subprocess.CompletedProcess(cmd, 1, b"", b"Too few characters")
        return subprocess.CompletedProcess(cmd, 0, b"tekst", b"")

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    assert extract._ocr(b"%PDF-1.4") == "tekst"
    assert calls[-1][calls[-1].index("--psm") + 1] == "6"
