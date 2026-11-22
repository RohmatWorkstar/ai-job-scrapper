"""CV text extraction for PDF, DOCX and TXT files."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.core.exceptions import CVError, ScannedPDFError
from src.cv.models import ParsedCV

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_CV_BYTES = 15 * 1024 * 1024
MIN_TEXT_CHARS = 80


def list_cv_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    files = [
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith(("~$", "."))
    ]
    return sorted(files, key=lambda p: p.name.lower())


def _normalise(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[​-‏﻿]", "", text)
    text = text.replace("•", "•").replace("", "•").replace("●", "•")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _parse_pdf(path: Path) -> tuple[str, int, list[str]]:
    try:
        import pymupdf  # PyMuPDF >= 1.24
    except ImportError:  # pragma: no cover - older installs
        import fitz as pymupdf  # type: ignore[no-redef]
    warnings: list[str] = []
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001 - library raises various types
        raise CVError(f"Cannot open PDF '{path.name}': {exc}") from exc
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise CVError(f"PDF '{path.name}' is password-protected. Provide an unprotected copy.")
        pages_text: list[str] = []
        image_pages = 0
        for page in doc:
            txt = page.get_text("text", sort=True) or ""
            pages_text.append(txt)
            if len(txt.strip()) < 20 and page.get_images(full=False):
                image_pages += 1
        page_count = doc.page_count
    finally:
        doc.close()
    text = "\n".join(pages_text)
    if len(text.strip()) < MIN_TEXT_CHARS:
        if image_pages:
            raise ScannedPDFError(
                f"'{path.name}' appears to be a scanned / image-only PDF ({image_pages} of {page_count} pages "
                "contain only images). No text layer is available. Run OCR on it or provide a DOCX/TXT version."
            )
        raise CVError(f"'{path.name}' contains almost no extractable text.")
    if image_pages:
        warnings.append(f"{image_pages} page(s) contain only images; their content could not be read.")
    return text, page_count, warnings


def _parse_docx(path: Path) -> tuple[str, int, list[str]]:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise CVError("python-docx is not installed") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise CVError(f"Cannot open DOCX '{path.name}': {exc}") from exc
    parts: list[str] = []
    for section in document.sections:
        for p in section.header.paragraphs:
            if p.text.strip():
                parts.append(p.text)
    for p in document.paragraphs:
        parts.append(p.text)
    for table in document.tables:
        for row in table.rows:
            seen: set[int] = set()
            cells = []
            for cell in row.cells:
                if id(cell._tc) in seen:  # merged cells repeat
                    continue
                seen.add(id(cell._tc))
                if cell.text.strip():
                    cells.append(cell.text.strip())
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise CVError(f"'{path.name}' contains almost no text (text boxes/images are not supported).")
    return text, 1, []


def _parse_txt(path: Path) -> tuple[str, int, list[str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            text = raw.decode(enc)
            warnings = [] if enc in ("utf-8-sig", "utf-16") else [f"Decoded using {enc} encoding."]
            if len(text.strip()) < MIN_TEXT_CHARS:
                raise CVError(f"'{path.name}' contains almost no text.")
            return text, 1, warnings
        except UnicodeDecodeError:
            continue
    raise CVError(f"Cannot decode '{path.name}'.")  # pragma: no cover - latin-1 always decodes


def parse_cv(path: Path) -> ParsedCV:
    """Extract text from a CV file. Raises CVError/ScannedPDFError with a clear message."""
    if not path.exists() or not path.is_file():
        raise CVError(f"CV file not found: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise CVError(f"Unsupported CV format '{suffix}'. Supported: PDF, DOCX, TXT.")
    size = path.stat().st_size
    if size == 0:
        raise CVError(f"'{path.name}' is empty.")
    if size > MAX_CV_BYTES:
        raise CVError(f"'{path.name}' is larger than 15 MB.")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise CVError(f"'{path.name}' is not a valid PDF file.")
        text, pages, warnings = _parse_pdf(path)
    elif suffix == ".docx":
        if not data.startswith(b"PK"):
            raise CVError(f"'{path.name}' is not a valid DOCX file.")
        text, pages, warnings = _parse_docx(path)
    else:
        text, pages, warnings = _parse_txt(path)
    return ParsedCV(filename=path.name, file_format=suffix[1:], text=_normalise(text), pages=pages, sha256=sha,
                    warnings=warnings)
