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
    # Template check: no repeated title in role_sentence, no unnecessary employer drop
    tpl = gen.template_email(job, profile, doc.identity, matched_skills=["Python"], doc=doc, language="en")
    assert "3 years of" in tpl.body
    assert "Dynamic Labs" in tpl.body and "Acme" not in tpl.body  # target company in opening; employer not dropped

    # AI email check: honors email_language="en" despite analysis.language="id", passes dates & projects, no Dynamic false positive
    email = asyncio.run(gen.ai_email(job, analysis, profile, doc, ["Python", "PostgreSQL"]))
    assert email.method == "ai"
    assert email.language == "en"
    assert "Jan 2022 - Present" in captured_prompt[0]
    assert "QueueKit" in captured_prompt[0]
    assert "professional_experience" in captured_prompt[0]
    assert "role_category" in captured_prompt[0]

    # Cover note check (Web/ATS mode)
    note = asyncio.run(gen.cover_note(job, analysis, profile, doc, ["Python", "PostgreSQL"]))
    assert note.method == "ai"
    assert "Dynamic Labs" in note.body
    assert "attached" not in note.body.lower()


def test_real_pdf_multiline_skills_and_wrapped_bullets_regression(settings) -> None:
    """Regression from real PDF CV: standalone skill headings, wrapped uppercase/role-word continuations, NBSP."""
    from src.cv.analyzer import CVAnalyzer
    from src.cv.document import structure_cv
    from src.cv.models import ParsedCV
    from src.cv.parser import _normalise

    raw = (
        "ROHMAT SUPRIYADI\n"
        "Fullstack Engineer\n"
        "rohmats009@gmail.com \u00a0|\u00a0 +62-853-1096-6263\n"
        "PROFESSIONAL EXPERIENCE\n"
        "Developer Supervisor — PT Pacific Cross\n"
        "Mar 2025 – Present\n"
        "• Led the engineering team in developing HR and Payroll (React, NestJS) integrated with tax\n"
        "API data (saving ~USD 100,000/year in operational costs).\n"
        "• Built an internal Express.js email service (cutting monthly operational\n"
        "costs by 40%), implemented MFA, and frontend validation, and authored\n"
        "technical documentation to boost velocity by 30%.\n"
        "PROFESSIONAL PROJECTS\n"
        "PRISMAX — HR Management System\n"
        "• Developed attendance calculations, multi-level approval\n"
        "workflows (from staff to CEO), and reporting\n"
        "modules.\n"
        "TECHNICAL SKILLS\n"
        "Programming Languages\n"
        "JavaScript, TypeScript, Python, Golang\n"
        "Database & Performance\n"
        "PostgreSQL, Redis, Data Modeling, Query\n"
        "Optimization, Caching\n"
        "SOFT SKILLS\n"
        "Leadership \u00a0•\u00a0 Teamwork\n"
        "EDUCATION\n"
        "B.Sc. in IT — Example Univ\n"
        "2019 – 2023 \u00a0|\u00a0 GPA: 3.98 / 4.00\n"
    )
    cv = ParsedCV(filename="real.pdf", file_format="pdf", text=_normalise(raw), pages=1, sha256="z")
    profile = CVAnalyzer(settings).analyze(cv)
    doc = structure_cv(cv, profile)
    assert len(doc.experience) == 1
    assert len(doc.experience[0].bullets) == 2
    assert "API data (saving ~USD 100,000/year in operational costs)." in doc.experience[0].bullets[0]
    assert "frontend validation" in doc.experience[0].bullets[1] and "velocity by 30%." in doc.experience[0].bullets[1]
    assert len(doc.project_entries) == 1
    assert "workflows (from staff to CEO)" in doc.project_entries[0].bullets[0]
    assert doc.languages == []
    groups = dict(doc.skill_groups)
    assert groups["Programming Languages"] == ["JavaScript", "TypeScript", "Python", "Golang"]
    assert "Query Optimization" in groups["Database & Performance"]
    assert groups["Soft Skills"] == ["Leadership", "Teamwork"]
    assert all("\u00a0" not in item for _, items in doc.skill_groups for item in items)


def test_human_email_copy_and_clean_test_redirect(settings) -> None:
    """Human-style email personalization (personal greeting, ID bridge, no robotic boilerplate) + tag_subject=False."""
    from email.message import EmailMessage
    from src.applications.email_service import TestRedirectTransport, TransportResult

    gen = EmailGenerator(settings)
    profile = CandidateProfile(
        current_title="Fullstack Engineer",
        experience_years=3.0,
        skills=["React", "NestJS", "Redis", "TypeScript", "Python", "FastAPI"],
    )
    doc = CVDocument(
        identity=CandidateIdentity(
            name="Rohmat Supriyadi",
            email="rohmat@example.com",
            phone="+62-811-0000-0000",
            links=["rohmat.dev", "github.com/rohmat", "linkedin.com/in/rohmat"],
        ),
        headline="Fullstack Engineer",
        experience=[
            ExperienceEntry(
                "Developer Supervisor",
                "PT Pacific Cross",
                "Mar 2025 - Present",
                bullets=["Led the engineering team in developing HR and Payroll (React, NestJS) with Redis caching."],
            )
        ],
        project_entries=[
            ExperienceEntry(
                "PAYROLL SYSTEM",
                "HR Integration",
                "",
                bullets=["Built payroll API with React, NestJS and Redis."],
            )
        ],
        skills=["React", "NestJS", "Redis", "TypeScript", "Python", "FastAPI"],
        source_text="3+ years Developer Supervisor PT Pacific Cross React NestJS Redis TypeScript Python FastAPI",
    )

    # Personal greeting from recipient first name + Hacker News source hook + no robotic phrase
    hn_job = JobOpportunity(
        job_id="hn1",
        job_title="Full Stack Engineer",
        company_name="RAIDS AI",
        source_type=SourceType.COMMUNITY.value,
        source_name="Hacker News",
        source_url="https://news.ycombinator.com/item?id=1",
        application_emails=["rita@raidsai.ai"],
        job_description="Looking for a full stack engineer with React, TypeScript, and Python.",
        required_skills=["React", "TypeScript", "Python"],
    )
    em_en = gen.template_email(hn_job, profile, doc.identity, matched_skills=["React", "TypeScript", "Python"],
                               doc=doc, recipient_email="rita@raidsai.ai")
    assert em_en.body.startswith("Dear Rita,")
    assert "PT Pacific Cross" not in em_en.body  # no unnecessary employer name-dropping
    assert "which is part of this role's requirements" not in em_en.body
    assert "Day-to-day I've worked directly with" not in em_en.body
    assert "Reaching out about" not in em_en.body
    assert "Portfolio: rohmat.dev" in em_en.body
    assert "GitHub: github.com/rohmat" in em_en.body
    assert "LinkedIn: linkedin.com/in/rohmat" in em_en.body

    # Indonesian job with English CV gets natural Bahasa Indonesia accomplishments without employer dumping
    id_job = JobOpportunity(
        job_id="id1",
        job_title="Backend Engineer",
        company_name="Flip",
        source_type=SourceType.JOB_PORTAL.value,
        source_name="Glints Indonesia",
        source_url="https://flip.id/careers/1",
        application_emails=["talent@flip.id"],
        job_description="Kami mencari Backend Engineer berpengalaman dengan NestJS, React, dan Redis.",
        required_skills=["NestJS", "React", "Redis"],
    )
    em_id = gen.template_email(id_job, profile, doc.identity, matched_skills=["NestJS", "React", "Redis"],
                               doc=doc, recipient_email="talent@flip.id")
    assert em_id.body.startswith("Yth. Tim Rekrutmen,")
    assert "Flip" in em_id.body
    assert "PT Pacific Cross" not in em_id.body
    assert "sesuai dengan kebutuhan posisi ini" not in em_id.body

    # TestRedirectTransport(tag_subject=False) redirects To without adding "[TEST for ...]"
    sent_msgs: list[EmailMessage] = []

    class CaptureTransport:
        name = "mock"

        async def send(self, msg: EmailMessage) -> TransportResult:
            sent_msgs.append(msg)
            return TransportResult(message_id="<m1>", transport="mock")

    redirect = TestRedirectTransport(CaptureTransport(), "tester@example.com", tag_subject=False)
    msg = EmailMessage()
    msg["To"] = "talent@flip.id"
    msg["Subject"] = "Lamaran – Backend Engineer – Rohmat Supriyadi"
    msg.set_content("Yth. Tim Rekrutmen,")
    asyncio.run(redirect.send(msg))
    assert sent_msgs[0]["To"] == "tester@example.com"
    assert sent_msgs[0]["X-Original-To"] == "talent@flip.id"
    assert sent_msgs[0]["Subject"] == "Lamaran – Backend Engineer – Rohmat Supriyadi"
    assert "TEST" not in sent_msgs[0]["Subject"]


def test_five_job_categories_produce_distinct_factual_emails(settings) -> None:
    """Validate 5 distinct job categories (Fullstack, Backend, Frontend, Software Engineer, AI Integration)."""
    from src.applications.email_generator import detect_role_focus
    from src.applications.fact_check import check_free_text
    from src.applications.job_analyzer import JobAnalyzer

    gen = EmailGenerator(settings)
    analyzer = JobAnalyzer(settings)
    profile = CandidateProfile(
        current_title="Fullstack Engineer",
        experience_years=3.0,
        skills=[
            "JavaScript", "TypeScript", "Python", "PHP", "React", "Next.js", "Tailwind CSS",
            "Node.js", "Express", "NestJS", "Laravel", "FastAPI", "PostgreSQL", "Redis",
            "Docker", "REST API", "Microservices", "Figma",
        ],
    )
    doc = CVDocument(
        identity=CandidateIdentity(
            name="Rohmat Supriyadi",
            email="rohmats009@gmail.com",
            phone="+62-853-1096-6263",
            links=[
                "rohmat-supriyadi.netlify.app",
                "github.com/RohmatWorkstar",
                "linkedin.com/in/rohmat-supriyadi-848877124",
            ],
        ),
        headline="Fullstack Engineer",
        experience=[
            ExperienceEntry(
                "Developer Supervisor",
                "PT Pacific Cross",
                "Mar 2025 – Present",
                bullets=[
                    "Refined insurance claim calculation logic and resolved critical overpayment bugs (reducing financial losses by 80%), while automating real-time processing for tens of thousands of policy records.",
                    "Led the engineering team in developing the HR Management and Payroll System (React, Laravel, NestJS) featuring salary calculations integrated with attendance and tax API data (saving ~USD 100,000/year in operational costs).",
                    "Built an internal Express.js email service to replace third-party providers (cutting monthly operational costs by 40%), implemented MFA, data encryption/decryption, and frontend validation.",
                ],
            ),
            ExperienceEntry(
                "Fullstack Engineer",
                "PT Media Telekomunikasi",
                "Mar 2024 – Sep 2024",
                bullets=[
                    "Led the UI redesign of FE Unified and FE Manager projects to achieve 100% cross-device compatibility (mobile, tablet, desktop).",
                    "Architected and implemented a KrakenD-based API Gateway to enable more scalable microservices orchestration.",
                    "Developed robust backend REST APIs for real-time data synchronization between servers and user interfaces.",
                ],
            ),
            ExperienceEntry(
                "Frontend Developer",
                "PT Abbauf Konsultan",
                "Aug 2022 – Dec 2022",
                bullets=[
                    "Translated Figma designs into pixel-perfect, responsive frontend code tailored to user needs.",
                ],
            ),
        ],
        project_entries=[
            ExperienceEntry(
                "SNC",
                "Security and Connectivity",
                "",
                bullets=[
                    "Integrated Google Gemini API and Cybersecurity API for automated analysis, recommendations, and security features, supported by REST API development and a responsive web interface.",
                ],
            ),
        ],
        skills=list(profile.skills),
        source_text=(
            "3+ years Developer Supervisor Fullstack Engineer React TypeScript Next.js Tailwind CSS Node.js Express "
            "NestJS Laravel FastAPI Python PHP PostgreSQL Redis Docker Git GitHub REST API Microservices Figma KrakenD "
            "Google Gemini API Cybersecurity API 80% 100,000 40% 100% MFA encryption "
            "Refined Led Built Architected Developed Translated Integrated"
        ),
    )

    specs = [
        ("Fullstack Developer", "Northstar Cloud", ["React", "TypeScript", "Node.js", "PostgreSQL"], "fullstack",
         "Build end-to-end SaaS features with React, TypeScript, Node.js, and PostgreSQL."),
        ("Backend Developer", "PayWave Tech", ["Node.js", "Express", "PostgreSQL", "Redis", "Microservices"], "backend",
         "Architect scalable REST APIs, KrakenD API Gateway microservices, and Redis caching."),
        ("Frontend Developer", "Lumina Studio", ["React", "TypeScript", "Next.js", "Tailwind CSS", "Figma"], "frontend",
         "Build responsive cross-device UI with React, TypeScript, Next.js, Tailwind CSS, and Figma."),
        ("Software Engineer", "Veridian Systems", ["TypeScript", "Node.js", "PostgreSQL", "Docker"], "software_engineering",
         "Own system architecture, production debugging, security, and end-to-end engineering."),
        ("AI Integration Developer", "SynthFlow AI", ["Python", "FastAPI", "TypeScript", "React"], "ai_integration",
         "Integrate LLM APIs (Gemini/OpenAI), build AI workflows, and connect backend REST APIs."),
    ]
    bodies: list[str] = []
    for title, company, reqs, expected_focus, desc in specs:
        j = JobOpportunity(
            job_id=f"role-{expected_focus}",
            job_title=title,
            company_name=company,
            source_type=SourceType.ATS.value,
            source_name="ATS",
            source_url=f"https://{expected_focus}.test/1",
            job_description=desc,
            required_skills=reqs,
        )
        analysis = analyzer.analyze_deterministic(j)
        assert detect_role_focus(j, analysis) == expected_focus
        em = gen.template_email(j, profile, doc.identity, doc=doc, analysis=analysis)
        bodies.append(em.body)
        # Concise relevant subject
        assert title in em.subject and "Rohmat Supriyadi" in em.subject
        # No unnecessary previous employer names
        for emp in ("PT Pacific Cross", "PT Media Telekomunikasi", "PT Abbauf Konsultan"):
            assert emp not in em.body
        # Valid contact + labeled links
        assert "Portfolio: rohmat-supriyadi.netlify.app" in em.body
        assert "GitHub: github.com/RohmatWorkstar" in em.body
        assert "LinkedIn: linkedin.com/in/rohmat-supriyadi-848877124" in em.body
        # Target word budget (excluding signature lines)
        main_body = em.body.split("\n\nBest regards,")[0].split("\n\nKind regards,")[0]
        assert 90 <= len(main_body.split()) <= 185
        # Zero fabricated claims or AI clichés
        checked = main_body.replace(title, " ").replace(company, " ")
        assert check_free_text(checked, doc.source_text, gen.detector, allowed_numbers={"3"}) == []

    # Verify role-specific selection actually differs across categories
    assert "100,000" in bodies[0]                                  # Fullstack -> HR/Payroll system
    assert "KrakenD" in bodies[1]                                  # Backend -> API Gateway / microservices
    assert "Figma" in bodies[2] and "cross-device" in bodies[2]    # Frontend -> Figma + UI redesign
    assert "80%" in bodies[3]                                      # Software Engineer -> claim logic/bug fix
    assert "In a project (SNC" in bodies[4] and "Gemini" in bodies[4]  # AI Integration -> project clearly labeled
    assert len(set(bodies)) == 5
