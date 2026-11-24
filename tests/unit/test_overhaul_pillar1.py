"""Regression tests for Pillar 1 (security, privacy, extraction, verification, matching, transport)."""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
import pytest

from src.ai.provider_manager import AIManager
from src.applications.contacts import select_recipient
from src.applications.gmail_client import GmailSendingLimitError
from src.applications.smtp_client import SmtpClient
from src.cli.questions import parse_salary_input
from src.core.exceptions import UnsafeURLError
from src.core.models import JobOpportunity, RawDiscovery, SearchConfig, SourceType, VerificationStatus
from src.cv.models import CandidateProfile
from src.cv.profile_builder import merge_ai_profile
from src.extraction.content_parser import SkillDetector, detect_seniority, parse_salary
from src.extraction.job_normalizer import JobNormalizer, ai_extract
from src.intelligence.matching import Matcher
from src.intelligence.relevance import Prefilter
from src.intelligence.verification import Verifier
from src.utils.http_client import SafeHttpClient
from src.utils.logger import JsonFormatter, register_secrets
from src.utils.validators import clean_text, validate_url_syntax


def test_json_formatter_handles_long_ints_and_floats_without_crashing() -> None:
    register_secrets(["SUPER_SECRET_TOKEN_XYZ"])
    fmt = JsonFormatter()
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello user@example.com SUPER_SECRET_TOKEN_XYZ",
        args=(),
        exc_info=None,
    )
    rec.data = {
        "job_id": 123456789012,
        "ratio": 0.123456789123,
        "email": "hr@acme.test",
        "secret": 'quoted "SUPER_SECRET_TOKEN_XYZ"',
        "nested": [9876543210, {"phone": "+62 812-3456-7890"}],
    }
    raw = fmt.format(rec)
    parsed = json.loads(raw)
    assert parsed["data"]["job_id"] == 123456789012
    assert parsed["data"]["ratio"] == pytest.approx(0.123456789123)
    assert parsed["data"]["email"] == "<email>"
    assert "SUPER_SECRET_TOKEN_XYZ" not in raw
    assert parsed["data"]["nested"][0] == 9876543210
    assert parsed["data"]["nested"][1]["phone"] == "<phone>"


def test_validate_url_syntax_handles_malformed_ipv6() -> None:
    with pytest.raises(UnsafeURLError):
        validate_url_syntax("http://[::1/bad")


def test_clean_text_repairs_common_mojibake() -> None:
    assert clean_text("Itâ€™s a greatÂ\xa0roleâ€”join us!") == "It’s a great role—join us!"


def test_http_client_does_not_retry_non_transient_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    async def fake_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    async def fake_sleep(_s: float) -> None:
        return None

    async def run() -> None:
        client = SafeHttpClient(
            user_agent="TestBot/1.0",
            retry_attempts=3,
            transport=httpx.MockTransport(handler),
            resolver=fake_resolver,
            sleep=fake_sleep,
        )
        try:
            res = await client.get("https://example.org/file.pdf", respect_robots=False)
            assert res.error_kind == "content_type"
            assert calls == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_merge_ai_profile_blocks_fabricated_taxonomy_skills(settings) -> None:
    det = SkillDetector(settings.taxonomy)
    cv_text = "Backend Engineer. Built REST APIs with Python and PostgreSQL. Managed K8s clusters."
    base = CandidateProfile(skills=["Python", "PostgreSQL"])
    ai_payload = {
        "skills": [
            {"name": "Rust", "category": "programming_language", "evidence": "Built REST APIs with Python"},
            {"name": "Kubernetes", "category": "tool", "evidence": "Managed K8s clusters"},
        ],
        "soft_skills": "not-a-list-string",
        "certifications": "AWS Certified",
    }
    merged = merge_ai_profile(base, ai_payload, cv_text, det)
    assert "Rust" not in merged.skills
    assert "Rust" in merged.transferable_skills
    assert "Kubernetes" in merged.skills


def test_salary_parser_guards_against_funding_and_monthly_suffix() -> None:
    s_mo = parse_salary("Intern stipend: $7900/mo")
    assert s_mo is not None
    assert s_mo.minimum == 7900.0 and s_mo.currency == "USD" and s_mo.period == "month"

    assert parse_salary("We raised $250M in Series D funding") is None
    assert parse_salary("Over $781M processed for 5 million customers") is None
    assert parse_salary("Company valuation reached $180m this year") is None
    assert parse_salary("Melayani lebih dari 5 juta pengguna di Indonesia") is None

    s_id = parse_salary("Gaji 10 - 15 juta per bulan")
    assert s_id is not None and s_id.minimum == 10_000_000 and s_id.maximum == 15_000_000 and s_id.currency == "IDR"

    s_startup = parse_salary("Salary: $120k - $150k/year at a Series B startup")
    assert s_startup is not None and s_startup.minimum == 120_000 and s_startup.maximum == 150_000


def test_seniority_parser_ignores_reporting_lines_in_description() -> None:
    desc = "You will report directly to the VP of Engineering and collaborate with the Engineering Manager and Tech Lead."
    assert detect_seniority("Back-end Engineer", desc) == "unknown"
    assert detect_seniority("Senior Software Engineer, Backend", desc) == "senior"
    assert detect_seniority("Software Engineer", "We are seeking a senior backend engineer to own our APIs.") == "senior"
    assert detect_seniority("Software Engineer", "Open for fresh graduate and entry-level applicants.") == "entry"


def test_verifier_does_not_false_flag_indonesian_biaya_benefits(settings) -> None:
    v = Verifier(settings)
    legit = JobOpportunity(
        job_id="id1",
        job_title="Backend Developer",
        company_name="Startup Nusantara",
        source_type=SourceType.JOB_PORTAL.value,
        source_name="Portal",
        source_url="https://example.test/job/1",
        listed_in_live_feed=True,
        job_description="Tunjangan biaya kesehatan dan biaya transportasi ditanggung perusahaan. Kirim CV ke hr.nusantara@gmail.com",
    )
    assert v.verify(legit).status != VerificationStatus.POSSIBLE_SCAM

    scam = JobOpportunity(
        job_id="id2",
        job_title="Admin",
        company_name="Fake",
        source_type=SourceType.JOB_PORTAL.value,
        source_name="Portal",
        source_url="https://example.test/job/2",
        job_description="Wajib transfer biaya pendaftaran dulu. Hubungi loker123@gmail.com",
    )
    assert v.verify(scam).status == VerificationStatus.POSSIBLE_SCAM


def test_matcher_normalizes_salary_periods(settings) -> None:
    profile = CandidateProfile(current_title="Backend Engineer", target_positions=["Backend Engineer"], skills=["Python"])
    cfg_usd = SearchConfig(min_salary=90_000, salary_currency="USD")
    m_usd = Matcher(settings, profile, cfg_usd)
    hourly_job = JobOpportunity(
        job_id="h1",
        job_title="Backend Engineer",
        source_type=SourceType.ATS.value,
        source_name="ATS",
        source_url="https://a.test/1",
        salary_min=50,
        salary_currency="USD",
        salary_period="hour",
    )
    m_usd.apply(hourly_job)
    assert "meets the minimum" in hourly_job.match.salary_assessment

    cfg_idr = SearchConfig(min_salary=15_000_000, salary_currency="IDR")
    m_idr = Matcher(settings, profile, cfg_idr)
    yearly_idr = JobOpportunity(
        job_id="y1",
        job_title="Backend Engineer",
        source_type=SourceType.ATS.value,
        source_name="ATS",
        source_url="https://a.test/2",
        salary_min=240_000_000,
        salary_currency="IDR",
        salary_period="year",
    )
    m_idr.apply(yearly_idr)
    assert "meets the minimum" in yearly_idr.match.salary_assessment


def test_prefilter_blocks_non_tech_gtm_and_keeps_core_swe(settings) -> None:
    profile = CandidateProfile(
        current_title="Backend Engineer",
        target_positions=["Backend Engineer", "AI Engineer"],
        role_families=["backend", "ai_ml", "software_engineering"],
        skills=["Python", "AI", "Node.js"],
    )
    pf = Prefilter(settings, profile, SearchConfig())
    assert not pf.title_relevant("GTM Operations & Agentic Workflows")[0]
    assert not pf.title_relevant("Client Partner, Ads Solutions")[0]
    assert not pf.title_relevant("Forward Deployment Strategy and Operations")[0]
    assert pf.title_relevant("Backend Software Engineer, Growth Marketing Platform")[0]


def test_select_recipient_prefers_corporate_over_freemail() -> None:
    job = JobOpportunity(
        job_id="c1",
        job_title="Engineer",
        company_name="Acme",
        company_website="https://acme.id",
        source_type=SourceType.JOB_PORTAL.value,
        source_name="Portal",
        source_url="https://p.test/1",
        application_emails=["recruiter.acme@gmail.com", "careers@acme.id"],
    )
    res_strict = select_recipient(job, allow_freemail=False)
    assert res_strict.valid and res_strict.email == "careers@acme.id"
    res_loose = select_recipient(job, allow_freemail=True)
    assert res_loose.valid and res_loose.email == "careers@acme.id"


def test_smtp_recipients_refused_classifies_sending_limit() -> None:
    class FakeSMTP:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, _u, _p):
            pass

        def send_message(self, _msg):
            return {"hr@acme.test": (550, b"5.4.5 Daily user sending limit exceeded")}

    client = SmtpClient("me@gmail.com", "abcd efgh ijkl mnop", smtp_factory=FakeSMTP)
    msg = EmailMessage()
    msg["From"] = "me@gmail.com"
    msg["To"] = "hr@acme.test"
    msg["Subject"] = "Test"
    msg.set_content("Hello world body text")
    with pytest.raises(GmailSendingLimitError):
        asyncio.run(client.send(msg))


def test_parse_salary_input_indonesian_and_english_formats() -> None:
    assert parse_salary_input("10.000.000") == 10_000_000.0
    assert parse_salary_input("10,000,000") == 10_000_000.0
    assert parse_salary_input("10.000.000,50") == 10_000_000.5
    assert parse_salary_input("15000000") == 15_000_000.0
    assert parse_salary_input("") is None


def test_ai_extract_consumes_required_skills_and_instructions(settings) -> None:
    class FakeAI:
        available = True
        remaining = 5

        async def ask_json(self, *_a, **_kw):
            return {
                "items": [
                    {
                        "id": "0",
                        "kind": "job_posting",
                        "is_job_opportunity": True,
                        "required_skills": ["FastAPI", "NonExistentSkill"],
                        "application_instructions": "Please send your resume to hiring@startup.io",
                    }
                ]
            }

    d = RawDiscovery(
        source_type=SourceType.COMMUNITY,
        source_name="Community",
        source_url="https://c.test/1",
        title="Backend Engineer",
        retrieved_at=datetime.now(timezone.utc),
        text="We use FastAPI and PostgreSQL. Please send your resume to hiring@startup.io.",
    )
    det = SkillDetector(settings.taxonomy)
    updated = asyncio.run(ai_extract([d], FakeAI(), detector=det))  # type: ignore[arg-type]
    assert updated == 1
    assert "FastAPI" in d.raw.get("ai_required_skills", [])
    assert "NonExistentSkill" not in d.raw.get("ai_required_skills", [])
    assert "hiring@startup.io" in d.raw.get("contact_emails", [])

    job = JobNormalizer(settings).normalise(d)
    assert "FastAPI" in job.required_skills
    assert "hiring@startup.io" in job.application_emails
