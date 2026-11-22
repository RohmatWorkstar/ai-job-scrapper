from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import ContentKind, JobOpportunity, RelevanceCategory, SearchConfig, SourceType, VerificationStatus
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.discovery.query_generator import parse_preferences
from src.intelligence.deduplication import deduplicate
from src.intelligence.matching import Matcher
from src.intelligence.verification import Verifier
from src.utils.date_time import utc_now

NOW = utc_now()


def job(**kw) -> JobOpportunity:
    base = dict(job_id=kw.pop("job_id", "j1"), job_title="Backend Engineer", company_name="Contoh Teknologi",
                source_type=SourceType.ATS.value, source_name="Greenhouse (contohtek)",
                source_url="https://job-boards.greenhouse.io/contohtek/jobs/1", discovered_at=NOW)
    base.update(kw)
    return JobOpportunity(**base)


# ------------------------------------------------------------ dedup
def test_cross_source_duplicate_merged_and_sources_kept() -> None:
    ats = job(job_id="a", country="Indonesia", city="Jakarta", location="Jakarta, Indonesia", listed_in_live_feed=True,
              official_channel=True, external_id="greenhouse:contohtek:1",
              job_description="Build payment APIs with Node.js TypeScript PostgreSQL Docker in Jakarta for merchants")
    portal = job(job_id="b", source_type=SourceType.JOB_PORTAL.value, source_name="JobStreet",
                 source_url="https://www.jobstreet.co.id/job/987", country="Indonesia", city="Jakarta",
                 job_title="Backend Engineer", company_name="PT Contoh Teknologi Tbk",
                 job_description="Build payment APIs with Node.js TypeScript PostgreSQL Docker in Jakarta for merchants",
                 salary_min=18e6, salary_currency="IDR")
    social = job(job_id="c", source_type=SourceType.SOCIAL_MEDIA.value, source_name="LinkedIn Posts",
                 source_url="https://www.linkedin.com/posts/x", company_name="Contoh Teknologi",
                 job_title="Backend Engineer (Remote)", job_description="")
    unique, merged = deduplicate([portal, social, ats])
    assert merged == 2 and len(unique) == 1
    u = unique[0]
    assert u.source_type == SourceType.ATS.value  # official source is primary
    assert {s.source_url for s in u.additional_sources} == {portal.source_url, social.source_url}
    assert u.salary_min == 18e6  # missing info filled from secondary


def test_same_title_different_companies_not_merged() -> None:
    a = job(job_id="a", company_name="Alpha", source_url="https://a.test/1")
    b = job(job_id="b", company_name="Beta", source_url="https://b.test/1")
    unique, merged = deduplicate([a, b])
    assert merged == 0 and len(unique) == 2


def test_uncertain_duplicates_flagged_not_merged() -> None:
    a = job(job_id="a", country="Indonesia", source_type=SourceType.JOB_PORTAL.value, source_url="https://p.test/1")
    b = job(job_id="b", country="Singapore", source_type=SourceType.SEARCH_ENGINE.value, source_url="https://q.test/2")
    unique, merged = deduplicate([a, b])
    assert merged == 0 and len(unique) == 2
    assert unique[0].possible_duplicates and unique[1].possible_duplicates


def test_distinct_ats_postings_not_flagged() -> None:
    a = job(job_id="a", city="Seattle", external_id="greenhouse:stripe:1", source_url="https://g.test/1")
    b = job(job_id="b", city="Dublin", external_id="greenhouse:stripe:2", source_url="https://g.test/2")
    unique, _ = deduplicate([a, b])
    assert not unique[0].possible_duplicates


# ------------------------------------------------------------ verification
def test_verification_statuses(settings) -> None:
    v = Verifier(settings)
    live = job(listed_in_live_feed=True, official_channel=True)
    assert v.verify(live).status == VerificationStatus.VERIFIED_OPEN

    aggregator = job(source_type=SourceType.JOB_PORTAL.value, source_name="Jobicy", listed_in_live_feed=True)
    assert v.verify(aggregator).status == VerificationStatus.LIKELY_OPEN

    social = job(source_type=SourceType.SOCIAL_MEDIA.value, content_kind=ContentKind.SOCIAL_POST.value)
    assert v.verify(social).status == VerificationStatus.NEEDS_REVIEW

    search_only = job(source_type=SourceType.SEARCH_ENGINE.value, content_kind=ContentKind.SEARCH_RESULT.value)
    r = v.verify(search_only)
    assert r.status == VerificationStatus.UNVERIFIED and "search" in r.reason.lower()

    gone = job(source_type=SourceType.SEARCH_ENGINE.value, signals={"fetch": {"ok": False, "status": 404, "error": "HTTP 404"}})
    assert v.verify(gone).status == VerificationStatus.INACCESSIBLE

    closed = job(signals={"fetch": {"ok": True, "closed_markers": ["no longer accepting applications"]}}, page_fetched=True)
    assert v.verify(closed).status == VerificationStatus.CLOSED

    expired = job(signals={"fetch": {"ok": True, "valid_through": (NOW - timedelta(days=3)).isoformat()}}, page_fetched=True)
    assert v.verify(expired).status == VerificationStatus.CLOSED

    # HTTP 200 alone is never enough for VERIFIED_OPEN
    page_only = job(source_type=SourceType.SEARCH_ENGINE.value, page_fetched=True,
                    signals={"fetch": {"ok": True, "status": 200}, "job_signal": 0.2})
    assert v.verify(page_only).status == VerificationStatus.UNVERIFIED


def test_scam_detection_and_disclaimer(settings) -> None:
    v = Verifier(settings)
    scam = job(job_description="Earn money fast! Pay a registration fee of Rp 500.000 via transfer uang to start.")
    assert v.verify(scam).status == VerificationStatus.POSSIBLE_SCAM
    honest = job(listed_in_live_feed=True, official_channel=True,
                 job_description="Beware of fraud: we will never ask you to pay a fee during recruitment.")
    assert v.verify(honest).status == VerificationStatus.VERIFIED_OPEN


# ------------------------------------------------------------ matching
def _matcher(settings, sample_cv_path, **cfg):
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    config = SearchConfig(target_positions=["Fullstack Developer", "Backend Developer"], **cfg)
    return Matcher(settings, profile, config, parse_preferences(config.additional_preferences))


def test_matching_explanations(settings, sample_cv_path) -> None:
    m = _matcher(settings, sample_cv_path)
    good = job(job_title="Backend Developer", country="Indonesia", work_arrangement="hybrid", seniority="mid",
               required_skills=["Node.js", "PostgreSQL", "TypeScript", "Kubernetes"],
               published_at=NOW - timedelta(days=1), job_description="3+ years of experience")
    m.apply(good)
    assert good.relevance_category == RelevanceCategory.HIGHLY_RELEVANT.value
    assert 0 <= good.relevance_score <= 100 and float(good.relevance_score).is_integer()
    assert "Kubernetes" in good.match.missing_skills
    assert "Node.js" in good.match.matched_skills
    assert any("Matching skills" in e for e in good.match.explanation)

    sales = job(job_title="Senior Account Executive", country="Indonesia", work_arrangement="onsite")
    m.apply(sales)
    assert sales.relevance_category in (RelevanceCategory.NOT_RELEVANT.value, RelevanceCategory.NEEDS_MANUAL_REVIEW.value,
                                        RelevanceCategory.POTENTIALLY_RELEVANT.value)
    assert sales.relevance_score < good.relevance_score


def test_missing_info_is_kept_not_discarded(settings, sample_cv_path) -> None:
    m = _matcher(settings, sample_cv_path)
    sparse = job(job_title="Fullstack Developer")  # no salary, arrangement, date, skills
    m.apply(sparse)
    assert sparse.relevance_category != RelevanceCategory.NOT_RELEVANT.value
    assert "Work arrangement" in sparse.match.needs_verification
    assert "Publication date" in sparse.match.needs_verification
    assert sparse.match.salary_assessment == "Salary not disclosed"


def test_location_gate_blocks_highly_relevant(settings, sample_cv_path) -> None:
    m = _matcher(settings, sample_cv_path)
    eu_only = job(job_title="Backend Developer", work_arrangement="remote", location_restrictions=["Europe"],
                  required_skills=["Node.js", "PostgreSQL"], published_at=NOW, seniority="mid")
    m.apply(eu_only)
    assert eu_only.relevance_category != RelevanceCategory.HIGHLY_RELEVANT.value
    assert any("restricted" in eu_only.match.arrangement_assessment.lower() for _ in [0])


def test_negative_preference_and_salary(settings, sample_cv_path) -> None:
    m = _matcher(settings, sample_cv_path, additional_preferences="Avoid banking companies",
                 min_salary=20_000_000, salary_currency="IDR")
    a = job(job_title="Backend Developer", company_name="Bank Example", job_description="A banking company",
            salary_min=10_000_000, salary_currency="IDR", country="Indonesia")
    b = job(job_id="j2", job_title="Backend Developer", company_name="Startup", salary_min=25_000_000,
            salary_currency="IDR", country="Indonesia")
    m.apply(a)
    m.apply(b)
    assert a.relevance_score < b.relevance_score
    assert "below the minimum" in a.match.salary_assessment
    usd = job(job_id="j3", job_title="Backend Developer", salary_min=50_000, salary_currency="USD")
    m.apply(usd)
    assert "not converted" in usd.match.salary_assessment
