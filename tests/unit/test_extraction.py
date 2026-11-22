from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import ContentKind, RawDiscovery, SourceType
from src.extraction.content_parser import (
    CountryDetector,
    SkillDetector,
    detect_employment_type,
    detect_seniority,
    detect_work_arrangement,
    detect_years_required,
    job_signal_strength,
    parse_salary,
)
from src.extraction.html_extractor import extract_from_html, html_to_text
from src.extraction.job_normalizer import JobNormalizer, enrich_with_page, normalise_company
from tests.conftest import fixture_text

NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


def test_json_ld_job_posting_extracted() -> None:
    page = extract_from_html(fixture_text("career_page.html"), "https://careers.example-corp.test/jobs/fullstack-developer",
                             ["no longer available"])
    jp = page.primary_posting
    assert jp is not None
    assert jp.title == "Fullstack Developer"
    assert jp.company == "Example Corp"
    assert jp.country == "ID"
    assert jp.date_posted and jp.date_posted.date().isoformat() == "2026-11-19"
    assert jp.salary_currency == "IDR" and jp.salary_min == 15_000_000 and jp.salary_max == 22_000_000
    assert jp.identifier == "EC-FS-01"
    assert page.apply_links == ["https://careers.example-corp.test/jobs/fullstack-developer/apply"]
    assert not page.closed_markers
    assert "Home | Jobs" not in page.text  # nav stripped


def test_closed_page_and_relative_date() -> None:
    page = extract_from_html(fixture_text("closed_page.html"), "https://x.test/job", ["this job is no longer available"])
    assert page.closed_markers == ["this job is no longer available"]
    assert page.published_hint is not None
    assert not page.job_postings


def test_html_to_text_unescapes_greenhouse_content() -> None:
    txt = html_to_text("&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Node.js&lt;/li&gt;&lt;/ul&gt;")
    assert "Hello world" in txt and "Node.js" in txt and "<" not in txt


def test_skill_detection_boundaries(settings) -> None:
    det = SkillDetector(settings.taxonomy)
    found = det.detect_names("We use Java, Golang, React.js and Node.js. JavaScript is a plus. Go to our site.")
    assert {"Java", "Go", "React", "Node.js", "JavaScript"} <= set(found)
    assert "Go" not in det.detect_names("Go to the office every day and Express your ideas")
    assert det.canonical("nodejs") == "Node.js"
    assert det.canonical("Postgres") == "PostgreSQL"


def test_salary_parsing() -> None:
    s = parse_salary("Salary: IDR 18.000.000 - 25.000.000 per month")
    assert (s.minimum, s.maximum, s.currency, s.period) == (18_000_000, 25_000_000, "IDR", "month")
    s = parse_salary("Gaji Rp 8 - 12 juta")
    assert s.currency == "IDR" and s.minimum == 8_000_000 and s.maximum == 12_000_000
    s = parse_salary("Compensation $120k - $150k a year")
    assert (s.minimum, s.maximum, s.currency, s.period) == (120_000, 150_000, "USD", "year")
    s = parse_salary("€60,000 - €75,000")
    assert s.currency == "EUR" and s.minimum == 60_000
    assert parse_salary("Competitive salary and great benefits") is None
    assert parse_salary("We have 5000 customers") is None


def test_arrangement_employment_seniority() -> None:
    assert detect_work_arrangement("Fully remote, work from anywhere") == "remote"
    assert detect_work_arrangement("Hybrid - 3 days in office") == "hybrid"
    assert detect_work_arrangement("Work from office (WFO) in Jakarta") == "onsite"
    assert detect_work_arrangement("This is not a remote role") == "onsite"
    assert detect_work_arrangement("Great team") == "unknown"
    assert detect_employment_type("Kontrak 12 bulan (PKWT)") == "contract"
    assert detect_employment_type("Magang Backend Developer") == "internship"
    assert detect_employment_type("Full-time position") == "full_time"
    assert detect_seniority("Senior Backend Engineer") == "senior"
    assert detect_seniority("Junior Frontend Developer") == "junior"
    assert detect_seniority("Engineering Manager") == "lead"
    assert detect_seniority("Backend Developer") == "unknown"
    assert detect_years_required("Minimum 3 years of experience with Node.js") == 3
    assert detect_years_required("5+ years professional experience") == 5


def test_country_detection(settings) -> None:
    cd = CountryDetector(settings.country_table())
    assert cd.detect("Jakarta Selatan, DKI Jakarta") == "Indonesia"
    assert cd.detect("Remote - US") == "United States"
    assert cd.detect("Let us know about you") is None  # lowercase 'us' is not a country
    assert cd.normalise("SG") == "Singapore"
    assert cd.detect("Berlin, Germany") == "Germany"


def test_job_signal() -> None:
    assert job_signal_strength("We are hiring a Backend Developer, apply now") > 0.6
    assert job_signal_strength("Average salary guide and interview tips for developers") < 0.3


def test_normalizer_and_evidence(settings) -> None:
    norm = JobNormalizer(settings)
    raw = RawDiscovery(source_type=SourceType.SEARCH_ENGINE, source_name="Web Search",
                       source_url="https://careers.example-corp.test/jobs/fullstack-developer",
                       title="Fullstack Developer - Example Corp Careers", retrieved_at=NOW, snippet="Apply now")
    page = extract_from_html(fixture_text("career_page.html"), raw.source_url, [])
    page.ok = True
    enrich_with_page(raw, page)
    job = norm.normalise(raw)
    assert job.job_title == "Fullstack Developer"
    assert job.company_name == "Example Corp"
    assert job.country == "Indonesia"
    assert job.employment_type == "full_time"
    assert job.salary_currency == "IDR" and job.salary_min == 15_000_000
    assert job.published_at and job.published_at.date().isoformat() == "2026-11-19"
    assert job.discovered_at == NOW
    assert job.application_url.endswith("/apply")
    assert job.source_url == raw.source_url  # discovery URL preserved separately
    assert "React" in job.required_skills
    fields = {e.field for e in job.evidence}
    assert {"job_title", "company_name", "published_at", "application_url", "salary"} <= fields


def test_social_post_date_kept_separate(settings) -> None:
    norm = JobNormalizer(settings)
    raw = RawDiscovery(source_type=SourceType.SOCIAL_MEDIA, source_name="LinkedIn Posts",
                       source_url="https://www.linkedin.com/posts/x_activity-1", title="We are hiring Backend Developer",
                       retrieved_at=NOW, snippet="We are hiring! Remote.", content_kind=ContentKind.SOCIAL_POST,
                       published_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                       published_at_evidence="search index date (2 days ago)")
    job = norm.normalise(raw)
    assert job.published_at is None
    assert job.post_created_at is not None
    assert any("search engine" in w for w in job.warnings)
    assert job.work_arrangement == "remote"


def test_company_normalisation() -> None:
    assert normalise_company("PT Contoh Teknologi Tbk") == normalise_company("Contoh Teknologi")
    assert normalise_company("Example Corp, Inc.") == "example"
