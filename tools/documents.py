"""Turn a contract file into plain text for the Scout: PDF, Word, text or Markdown.

Citations are verified against exactly this text, so what the model reads and what
the checker searches are always the same string.
"""

from __future__ import annotations

from pathlib import Path

CONTRACT_SUFFIXES = (".pdf", ".docx", ".md", ".txt")
MIN_READABLE_CHARS = 200


class DocumentError(ValueError):
    """A contract file that can't be read, with a message written for the person running the tool."""


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".doc":
        raise DocumentError(f"{path.name} is an old Word file. Open it in Word and use Save As > Word Document (.docx).")
    raise DocumentError(f"{path.name}: contracts must be PDF, Word (.docx), text or Markdown.")


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise DocumentError(f"Could not read {path.name} as a PDF ({exc}). If it is password protected, remove the password and try again.") from exc
    if len(text.strip()) < MIN_READABLE_CHARS:
        raise DocumentError(
            f"{path.name} has no readable text, so it is probably a scanned image. "
            "Run it through OCR (for example Adobe Acrobat > Scan & OCR > Recognize Text) and save it again."
        )
    return text


def _read_docx(path: Path) -> str:
    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocumentError(f"Could not read {path.name} as a Word document ({exc}).") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    text = "\n\n".join(p for p in parts if p.strip())
    if len(text.strip()) < MIN_READABLE_CHARS:
        raise DocumentError(f"{path.name} has almost no text in it. Check that it is the signed contract.")
    return text
