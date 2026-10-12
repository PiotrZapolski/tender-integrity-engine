"""Text extraction from tender opening-of-offers documents (PDF, DOCX, scanned PDF)."""

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

MIN_TEXT_CHARS = 200
PDFTOTEXT_TIMEOUT = 60
PDFTOPPM_TIMEOUT = 300
TESSERACT_TIMEOUT = 120


class UnsupportedDocument(Exception):
    """Raised for file extensions that cannot be extracted."""


@dataclass(frozen=True)
class Extracted:
    text: str
    method: str  # "pdftext" | "docx" | "ocr"


def extract_text(data: bytes, file_name: str) -> Extracted:
    ext = Path(file_name).suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return Extracted(text=_extract_docx(data), method="docx")
    raise UnsupportedDocument(f"unsupported document type: {file_name!r}")


def _extract_pdf(data: bytes) -> Extracted:
    text = _pdftotext(data)
    if len("".join(text.split())) >= MIN_TEXT_CHARS:
        return Extracted(text=text, method="pdftext")
    return Extracted(text=_ocr(data), method="ocr")


def _pdftotext(data: bytes) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=data,
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _page_number(path: Path) -> int:
    return int(path.stem.rsplit("-", 1)[1])


def _ocr(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        pdf_path = tmp_dir / "file.pdf"
        pdf_path.write_bytes(data)
        subprocess.run(
            ["pdftoppm", "-r", "300", "-png", str(pdf_path), str(tmp_dir / "page")],
            check=True,
            capture_output=True,
            timeout=PDFTOPPM_TIMEOUT,
        )
        pages = sorted(tmp_dir.glob("page-*.png"), key=_page_number)
        texts = []
        for page in pages:
            proc = subprocess.run(
                ["tesseract", str(page), "-", "-l", "pol", "--psm", "6"],
                check=True,
                capture_output=True,
                timeout=TESSERACT_TIMEOUT,
            )
            texts.append(proc.stdout.decode("utf-8", errors="replace"))
    return "\n\f\n".join(texts)


def _extract_docx(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(Paragraph(child, doc).text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, doc)
            for row in table.rows:
                lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)
