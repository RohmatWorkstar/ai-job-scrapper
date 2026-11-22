from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import CVError, ScannedPDFError
from src.cv.analyzer import CVAnalyzer, employment_intervals, total_years
from src.cv.parser import list_cv_files, parse_cv
from src.cv.profile_builder import merge_ai_profile
from src.extraction.content_parser import SkillDetector
from tests.conftest import FIXTURES


def _make_pdf(path: Path, text: str | None) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        y = 72
        for line in text.splitlines():
            page.insert_text((50, y), line, fontsize=9)
            y += 12
            if y > 780:
                page = doc.new_page()
                y = 72
    else:  # image-only page (simulated scan)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), 0)
        pix.clear_with(200)
        page.insert_image(pymupdf.Rect(50, 50, 300, 300), pixmap=pix)
    doc.save(str(path))
    doc.close()


def _make_docx(path: Path, text: str) -> None:
    import docx

    d = docx.Document()
    lines = text.splitlines()
    for line in lines[:-4]:
        d.add_paragraph(line)
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Skills"
    table.rows[0].cells[1].text = "Kubernetes, GraphQL"
    for line in lines[-4:]:
        d.add_paragraph(line)
    d.save(str(path))


def test_txt_cv_parsed(sample_cv_path: Path) -> None:
    cv = parse_cv(sample_cv_path)
    assert cv.file_format == "txt"
    assert "Fullstack Developer" in cv.text
    assert len(cv.sha256) == 64


def test_pdf_cv_multi_page(tmp_path: Path, sample_cv_path: Path) -> None:
    text = sample_cv_path.read_text(encoding="utf-8") * 3  # forces several pages
    p = tmp_path / "cv.pdf"
    _make_pdf(p, text)
    cv = parse_cv(p)
    assert cv.pages >= 2
    assert "PostgreSQL" in cv.text


def test_scanned_pdf_detected(tmp_path: Path) -> None:
    p = tmp_path / "scan.pdf"
    _make_pdf(p, None)
    with pytest.raises(ScannedPDFError):
        parse_cv(p)


def test_docx_cv_includes_tables(tmp_path: Path, sample_cv_path: Path) -> None:
    p = tmp_path / "cv.docx"
    _make_docx(p, sample_cv_path.read_text(encoding="utf-8"))
    cv = parse_cv(p)
    assert "Kubernetes, GraphQL" in cv.text
    assert "Fullstack Developer" in cv.text


def test_invalid_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(CVError):
        parse_cv(empty)
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all" * 20)
    with pytest.raises(CVError):
        parse_cv(fake)
    with pytest.raises(CVError):
        parse_cv(tmp_path / "missing.docx")
    odd = tmp_path / "cv.rtf"
    odd.write_text("x" * 200)
    with pytest.raises(CVError):
        parse_cv(odd)


def test_cp1252_txt(tmp_path: Path) -> None:
    p = tmp_path / "cv.txt"
    p.write_bytes(("Desenvolvedor Fullstack – São Paulo\n" + "React Node.js PostgreSQL experiência\n" * 5).encode("cp1252"))
    cv = parse_cv(p)
    assert "São Paulo" in cv.text
    assert cv.warnings


def test_list_cv_files(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "b.docx").write_bytes(b"PK")
    (tmp_path / "~$lock.docx").write_bytes(b"PK")
    (tmp_path / "README.md").write_text("x")
    assert [p.name for p in list_cv_files(tmp_path)] == ["a.pdf", "b.docx"]


def test_profile_from_cv(settings, sample_cv_path: Path) -> None:
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    assert profile.current_title == "Fullstack Developer"
    for skill in ("React", "Node.js", "Express", "PostgreSQL", "Prisma", "MongoDB", "Laravel", "Python"):
        assert skill in profile.skills
    assert "Kubernetes" not in profile.skills  # not in the CV -> never invented
    assert profile.experience_years is not None and 4.5 <= profile.experience_years <= 12
    assert profile.seniority_estimate in {"mid", "senior"}
    assert "Fullstack Developer" in profile.target_positions
    assert not any(t.lower().startswith("senior") for t in profile.target_positions)
    assert profile.certifications == ["AWS Certified Cloud Practitioner (2023)"]
    assert "English" in profile.languages and "Indonesian" in profile.languages
    assert "fintech" in profile.industry_experience
    assert profile.skill_evidence["Prisma"]


def test_employment_intervals_merge_overlaps() -> None:
    from datetime import date

    text = "Jan 2020 - Dec 2021\nJun 2021 – Present\n2015 - 2016 (education ignored when section split)"
    iv = employment_intervals(text, today=date(2024, 1, 1))
    years = total_years(iv)
    assert years is not None and 5.5 <= years <= 6.5


def test_ai_merge_rejects_fabrication(settings, sample_cv_path: Path) -> None:
    cv = parse_cv(sample_cv_path)
    base = CVAnalyzer(settings).analyze(cv)
    ai = {
        "current_title": "Principal Architect",  # not in CV -> ignored
        "skills": [
            {"name": "TypeScript", "category": "programming_language", "evidence": "React + TypeScript dashboards"},
            {"name": "Kubernetes", "category": "tool", "evidence": "Managed Kubernetes clusters"},  # invented
        ],
        "certifications": ["AWS Certified Cloud Practitioner (2023)", "Google Professional Cloud Architect"],
        "seniority_estimate": "lead",
        "target_positions": [{"title": "Senior Backend Engineer", "directness": "direct"},
                             {"title": "Technical Product Manager", "directness": "adjacent"}],
        "transferable_skills": [{"name": "NestJS", "reason": "Express experience"}],
        "summary": "Fullstack developer with fintech experience.",
    }
    merged = merge_ai_profile(base, ai, cv.text, SkillDetector(settings.taxonomy))
    assert merged.current_title == "Fullstack Developer"
    assert "Kubernetes" not in merged.skills and "Kubernetes" in merged.transferable_skills
    assert "Google Professional Cloud Architect" not in merged.certifications
    assert merged.seniority_estimate == base.seniority_estimate  # never raised
    assert "Backend Engineer" in merged.target_positions  # "Senior" stripped
    assert "Technical Product Manager" in merged.adjacent_positions
    assert "NestJS" in merged.transferable_skills and "NestJS" not in merged.skills
    assert merged.analysis_method == "deterministic+ai"


def test_fixture_dir_exists() -> None:
    assert (FIXTURES / "sample_cv.txt").exists()
