"""CV text extraction for PDF, DOCX and TXT files."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.core.exceptions import CVError, ScannedPDFError
from src.cv.models import ParsedCV

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt"}
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


_EXTENDED_BULLETS = re.compile(r"[●▪◦▸►‣⁃◆■➢➤✓✔→]")


def _normalise(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[​-‏﻿]", "", text)
    text = _EXTENDED_BULLETS.sub("•", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_pdf_page_text(page: object) -> str:
    """Extract PDF page text preserving reading order for single- and two-column layouts."""
    try:
        raw_blocks = page.get_text("blocks")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return page.get_text("text", sort=True) or ""  # type: ignore[attr-defined]
    blocks = [b for b in raw_blocks if len(b) >= 7 and b[6] == 0 and str(b[4] or "").strip()]
    if not blocks:
        return page.get_text("text", sort=True) or ""  # type: ignore[attr-defined]
    width = float(getattr(getattr(page, "rect", None), "width", 600.0) or 600.0)
    left = [b for b in blocks if b[2] <= width * 0.50]
    right = [b for b in blocks if b[0] >= width * 0.45 and b not in left]
    full = [b for b in blocks if b not in left and b not in right]
    if len(left) >= 3 and len(right) >= 3 and max(b[2] for b in left) <= min(b[0] for b in right) + 12:
        col_top = min(min(b[1] for b in left), min(b[1] for b in right))
        top_full = sorted((b for b in full if b[1] < col_top), key=lambda b: (b[1], b[0]))
        bot_full = sorted((b for b in full if b[1] >= col_top), key=lambda b: (b[1], b[0]))
        ordered = top_full + sorted(left, key=lambda b: (b[1], b[0])) + sorted(right, key=lambda b: (b[1], b[0])) + bot_full
        return "\n".join(str(b[4]).strip() for b in ordered)
    ordered = sorted(blocks, key=lambda b: (round(b[1] / 4.0), b[0]))
    return "\n".join(str(b[4]).strip() for b in ordered)


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
            txt = _extract_pdf_page_text(page)
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


def _pdf_links(path: Path) -> list[str]:
    """http(s) hyperlink targets in a PDF (e.g. "GitHub" / "LinkedIn" labels that hide the URL)."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf  # type: ignore[no-redef]
    out: list[str] = []
    try:
        doc = pymupdf.open(str(path))
        try:
            for page in doc:
                for link in page.get_links():
                    uri = str(link.get("uri") or "").strip()
                    if uri.lower().startswith(("http://", "https://")) and uri not in out:
                        out.append(uri)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - links are optional
        return []
    return out[:10]


def _docx_links(document: object) -> list[str]:
    """Extract http(s) hyperlink targets from DOCX document & header relationships."""
    out: list[str] = []
    parts = [getattr(document, "part", None)]
    for section in getattr(document, "sections", []) or []:
        header = getattr(section, "header", None)
        if header is not None:
            parts.append(getattr(header, "part", None))
    for part in parts:
        rels = getattr(part, "rels", None)
        if not rels:
            continue
        for rel in rels.values():
            reltype = str(getattr(rel, "reltype", "") or "").lower()
            target = str(getattr(rel, "target_ref", "") or getattr(rel, "_target", "") or "").strip()
            if "hyperlink" in reltype and target.lower().startswith(("http://", "https://")) and target not in out:
                out.append(target)
    return out[:10]


def _parse_docx(path: Path) -> tuple[str, int, list[str], list[str]]:
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
    return text, 1, [], _docx_links(document)


def _doc_converters() -> list[list[str]]:
    """Commands able to turn a legacy Word .doc into text, if installed on this machine."""
    out: list[list[str]] = []
    if shutil.which("antiword"):
        out.append(["antiword", "-w", "0"])
    for name in ("soffice", "libreoffice"):
        exe = shutil.which(name)
        if exe:
            out.append([exe])
            break
    return out


def _parse_doc(path: Path) -> tuple[str, int, list[str]]:
    """Legacy .doc (OLE2) via antiword or LibreOffice. Neither is a Python dependency."""
    converters = _doc_converters()
    if not converters:
        raise CVError(f"'{path.name}' is a legacy .doc file. Install antiword or LibreOffice, or save the CV as "
                      "PDF/DOCX, to use it.")
    errors: list[str] = []
    for cmd in converters:
        try:
            if cmd[0] == "antiword":
                proc = subprocess.run([*cmd, str(path)], capture_output=True, timeout=60, check=False)
                text = proc.stdout.decode("utf-8", errors="replace")
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    subprocess.run([cmd[0], "--headless", "--convert-to", "txt:Text", "--outdir", tmp, str(path)],
                                   capture_output=True, timeout=120, check=False)
                    out = Path(tmp) / (path.stem + ".txt")
                    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{Path(cmd[0]).name}: {exc}")
            continue
        if len(text.strip()) >= MIN_TEXT_CHARS:
            return text, 1, [f"Converted from .doc with {Path(cmd[0]).name}."]
        errors.append(f"{Path(cmd[0]).name}: no text produced")
    raise CVError(f"Cannot read '{path.name}': " + "; ".join(errors))


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
        raise CVError(f"Unsupported CV format '{suffix}'. Supported: PDF, DOCX, DOC, TXT.")
    size = path.stat().st_size
    if size == 0:
        raise CVError(f"'{path.name}' is empty.")
    if size > MAX_CV_BYTES:
        raise CVError(f"'{path.name}' is larger than 15 MB.")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    links: list[str] = []
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise CVError(f"'{path.name}' is not a valid PDF file.")
        text, pages, warnings = _parse_pdf(path)
        links = _pdf_links(path)
    elif suffix == ".docx":
        if not data.startswith(b"PK"):
            raise CVError(f"'{path.name}' is not a valid DOCX file.")
        text, pages, warnings, links = _parse_docx(path)
    elif suffix == ".doc":
        if not data.startswith(b"\xd0\xcf\x11\xe0"):
            raise CVError(f"'{path.name}' is not a valid Word .doc file.")
        text, pages, warnings = _parse_doc(path)
    else:
        text, pages, warnings = _parse_txt(path)
    return ParsedCV(filename=path.name, file_format=suffix[1:], text=_normalise(text), pages=pages, sha256=sha, links=links,
                    warnings=warnings)
