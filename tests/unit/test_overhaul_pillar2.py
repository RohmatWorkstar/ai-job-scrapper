"""Tests for Pillar 2 (unified CV document, parser upgrades, aligned CVMatcher, CVGenerator, EmailGenerator)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from src.ai.prompts import redact_personal
from src.applications import cv_document as cv_doc_facade
from src.applications.cv_generator import CVGenerator
from src.applications.cv_matcher import CVMatcher
from src.applications.email_generator import EmailGenerator
from src.applications.job_analyzer import JobAnalysis
from src.applications.models import CandidateIdentity
from src.core.models import JobOpportunity, SearchConfig, SourceType
from src.cv import document as cv_doc_canonical
from src.cv.document import CVDocument, ExperienceEntry, _verbatim_norm, parse_experience
from src.cv.models import CandidateProfile
from src.cv.parser import parse_cv
from src.discovery.query_generator import Preferences


def test_cv_document_facade_reexports_canonical_module() -> None:
    assert cv_doc_facade.CVDocument is cv_doc_canonical.CVDocument
    assert cv_doc_facade.structure_cv is cv_doc_canonical.structure_cv
    assert cv_doc_facade.parse_experience is cv_doc_canonical.parse_experience


def test_verbatim_norm_strips_extended_bullets_and_experience_guard() -> None:
    assert _verbatim_norm("▸ Built REST APIs with Node.js") == "built rest apis with node.js"
    section = (
        "Software Engineer - Alpha Corp (Jan 2023 - Present)\n"
        "▸ Built payment gateway in Node.js\n"
        "  Backend Developer - Beta Labs (Jan 2021 - Dec 2022)\n"
        "▸ Maintained PostgreSQL clusters\n"
    )
    entries = parse_experience(section)
    assert len(entries) == 2
    assert entries[0].title == "Software Engineer" and entries[0].company == "Alpha Corp"
    assert entries[1].title == "Backend Developer" and entries[1].company == "Beta Labs"


def test_ai_safe_dict_includes_dates_projects_and_redacts_name() -> None:
    doc = CVDocument(
        identity=CandidateIdentity(name="Budi Santoso", email="budi@example.id"),
        headline="Backend Engineer",
        summary="Budi Santoso builds distributed services.",
        experience=[ExperienceEntry("Backend Engineer", "Acme", "Jan 2022 - Present", bullets=["Built APIs"])],
        project_entries=[ExperienceEntry("PayFlow", "", "2023", bullets=["Built webhook processor in Go"])],
        skills=["Go", "PostgreSQL"],
        source_text="Budi Santoso Backend Engineer Acme Jan 2022 - Present Built APIs",
    )
    safe = doc.ai_safe_dict()
    assert safe["experience"][0]["dates"] == "Jan 2022 - Present"
    assert len(safe["project_entries"]) == 1
    assert "Budi Santoso" not in safe["summary"]
    assert "[name removed]" in redact_personal("Contact Budi Santoso at myportfolio.dev", extra_names=["Budi Santoso"],
                                              extra_domains=["myportfolio.dev"])


def test_docx_hyperlink_extraction(tmp_path: Path) -> None:
    path = tmp_path / "cv.docx"
    document = docx.Document()
    p = document.add_paragraph("Budi Santoso - Backend Engineer with 4 years of experience in Python, PostgreSQL, Docker and FastAPI.")
    part = p.part
    r_id = part.relate_to("https://github.com/budisantoso", docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "GitHub"
    run.append(t)
    hyperlink.append(run)
    p._p.append(hyperlink)
    document.add_paragraph("Experience: Software Engineer at Acme (2021 - Present). Built REST APIs with Python and PostgreSQL.")
    document.save(str(path))

    parsed = parse_cv(path)
    assert "https://github.com/budisantoso" in parsed.links


def test_cv_matcher_uses_project_entries_and_negative_preferences(settings) -> None:
    profile = CandidateProfile(
        current_title="Backend Engineer",
        target_positions=["Backend Engineer"],
        skills=["Python", "PostgreSQL", "Docker"],
        experience_years=4.0,
    )
    doc = CVDocument(
        headline="Backend Engineer",
        skills=["Python", "PostgreSQL", "Docker"],
        project_entries=[ExperienceEntry("Ledger Service", "", "", bullets=["Implemented idempotent PostgreSQL transactions"])],
        source_text="Backend Engineer Python PostgreSQL Docker Ledger Service Implemented idempotent PostgreSQL transactions",
    )
    job = JobOpportunity(
        job_id="m1",
        job_title="Backend Engineer",
        company_name="Mega Banking Corp",
        source_type=SourceType.ATS.value,
        source_name="ATS",
        source_url="https://a.test/1",
        work_arrangement="onsite",
        job_description="Banking platform using Python and PostgreSQL.",
        required_skills=["Python", "PostgreSQL"],
    )
    analysis = JobAnalysis(job_title="Backend Engineer", company="Mega Banking Corp",
                           required_skills=["Python", "PostgreSQL"], keywords=["ledger", "postgresql"])
    m_plain = CVMatcher(settings, profile)
    r_plain = m_plain.match(job, analysis, doc)
    assert "Ledger Service" in r_plain.relevant_projects

    cfg = SearchConfig(work_arrangements=["remote"])
    prefs = Preferences(negative=["banking"])
    m_aligned = CVMatcher(settings, profile, config=cfg, preferences=prefs)
    r_aligned = m_aligned.match(job, analysis, doc)
    assert r_aligned.overall_score < r_plain.overall_score
    assert any("excluded preference" in c.lower() for c in r_aligned.concerns)
    assert any("work arrangement" in c.lower() for c in r_aligned.concerns)


def test_cv_generator_reorders_skill_groups_and_overwrites_in_place(settings, tmp_path: Path) -> None:
    settings.raw.setdefault("applications", {})["generated_cv_directory"] = str(tmp_path)

    class FakeAI:
        available = True

        async def ask_json(self, *_a, **_kw):
            return {
                "summary": "Backend Engineer with 4 years working with React and PostgreSQL.",
                "skills_order": ["React", "PostgreSQL"],
                "bullets": [],
            }

    doc = CVDocument(
        identity=CandidateIdentity(name="Budi Santoso", email="budi@example.id"),
        headline="Backend Engineer",
        summary="Backend Engineer working with PostgreSQL and React.",
        experience=[ExperienceEntry("Backend Engineer", "Acme", "2022 - Present",
                                    bullets=["Built APIs with PostgreSQL and React (Next.js) for web clients."])],
        skills=["Docker", "PostgreSQL", "React (Next.js)"],
        skill_groups=[("Infra", ["Docker"]), ("Web", ["PostgreSQL", "React (Next.js)"])],
        source_text="Budi Santoso Backend Engineer Acme 2022 - Present Built APIs with PostgreSQL and React (Next.js) Docker",
    )
    analysis = JobAnalysis(job_title="Fullstack Engineer", company="Acme", required_skills=["React", "PostgreSQL"])
    match = CVMatcher(settings, CandidateProfile(skills=["Docker", "PostgreSQL", "React"])).match(
        JobOpportunity(job_id="g1", job_title="Fullstack Engineer", company_name="Acme",
                       source_type=SourceType.ATS.value, source_name="ATS", source_url="https://a.test/1"),
        analysis,
        doc,
    )
    gen = CVGenerator(settings, FakeAI())  # type: ignore[arg-type]
    tcv, pdf1 = asyncio.run(gen.generate(doc, analysis, match, company="Acme", title="Fullstack Engineer",
                                         app_id="app111", experience_years=4.0))
    assert tcv.skill_groups[0][0] == "Web"
    assert tcv.skill_groups[0][1][0] == "React (Next.js)"
    assert "4 years" in tcv.summary

    # Regenerating with existing_path overwrites in place with zero orphan files
    _, pdf2 = asyncio.run(gen.generate(doc, analysis, match, company="Acme", title="Fullstack Engineer",
                                       app_id="app111", experience_years=4.0, existing_path=pdf1))
    assert pdf2 == pdf1
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_email_generator_ai_payload_tone_and_cover_note(settings) -> None:
    captured_prompt: list[str] = []

    class FakeAI:
        available = True

        async def ask_json(self, purpose: str, prompt: str, **_kw):
            captured_prompt.append(prompt)
            if purpose == "cover_note":
                return {
                    "body": "Applying for Backend Engineer at Dynamic Labs. Currently a Backend Engineer at Acme "
                            "(Jan 2022 - Present), I built PostgreSQL APIs and Python workers in production."
                }
            return {
                "subject": "Application for Backend Engineer - Dynamic Labs",
                "body": "Dear Dynamic Labs Hiring Team,\n\nI am applying for the Backend Engineer role at Dynamic Labs. "
                        "Currently at Acme (Jan 2022 - Present), I built PostgreSQL APIs with Python. My CV is attached."
            }

    settings.raw.setdefault("applications", {})["email_language"] = "en"
    gen = EmailGenerator(settings, FakeAI())  # type: ignore[arg-type]
    profile = CandidateProfile(current_title="Backend Engineer", experience_years=3.0, skills=["Python", "PostgreSQL"])
    doc = CVDocument(
        identity=CandidateIdentity(name="Budi Santoso", email="budi@example.id"),
        headline="Backend Engineer",
        experience=[ExperienceEntry("Backend Engineer", "Acme", "Jan 2022 - Present",
                                    bullets=["Built PostgreSQL APIs and Python workers in production."])],
        project_entries=[ExperienceEntry("QueueKit", "", "2023", bullets=["Built Python job queue"])],
        skills=["Python", "PostgreSQL"],
        source_text="Budi Santoso Backend Engineer Acme Jan 2022 - Present Built PostgreSQL APIs and Python workers in production QueueKit",
    )
    job = JobOpportunity(
        job_id="e1",
        job_title="Backend Engineer",
        company_name="Dynamic Labs",  # "dynamic" is in BUZZWORDS; must NOT false-trigger!
        source_type=SourceType.ATS.value,
        source_name="ATS",
        source_url="https://dynamic.test/job/1",
        job_description="Dicari Backend Engineer dengan Python dan PostgreSQL.",
        required_skills=["Python", "PostgreSQL"],
    )
    analysis = JobAnalysis(job_title="Backend Engineer", company="Dynamic Labs",
                           required_skills=["Python", "PostgreSQL"], language="id")
    # Template check: no repeated title in role_sentence
    tpl = gen.template_email(job, profile, doc.identity, matched_skills=["Python"], doc=doc, language="en")
    assert "as a Backend Engineer" not in tpl.body  # already says "I'm currently a Backend Engineer at Acme"
    assert "with 3 years of experience" in tpl.body

    # AI email check: honors email_language="en" despite analysis.language="id", passes dates & projects, no Dynamic false positive
    email = asyncio.run(gen.ai_email(job, analysis, profile, doc, ["Python", "PostgreSQL"]))
    assert email.method == "ai"
    assert email.language == "en"
    assert "Jan 2022 - Present" in captured_prompt[0]
    assert "QueueKit" in captured_prompt[0]

    # Cover note check (Web/ATS mode)
    note = asyncio.run(gen.cover_note(job, analysis, profile, doc, ["Python", "PostgreSQL"]))
    assert note.method == "ai"
    assert "Dynamic Labs" in note.body
    assert "attached" not in note.body.lower()
