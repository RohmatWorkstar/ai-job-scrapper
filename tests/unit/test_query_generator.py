from __future__ import annotations

from src.core.models import SearchConfig, SourceType
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.discovery.query_generator import QueryGenerator, parse_preferences


def _plan(settings, sample_cv_path, budget=40, **cfg):
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    config = SearchConfig(target_positions=["Fullstack Developer", "Backend Developer", "Node.js Developer"], **cfg)
    return profile, QueryGenerator(settings).generate(profile, config, budget=budget)


def test_budget_respected_and_categories_covered(settings, sample_cv_path) -> None:
    _, plan = _plan(settings, sample_cv_path, budget=40)
    total = len(plan.all())
    assert 30 <= total <= 40
    for cat in (SourceType.JOB_PORTAL, SourceType.SOCIAL_MEDIA, SourceType.SEARCH_ENGINE, SourceType.ATS,
                SourceType.COMMUNITY, SourceType.COMPANY_CAREERS):
        assert plan.queries.get(cat), cat
    texts = [q.text for q in plan.all()]
    assert len(texts) == len(set(t.lower() for t in texts))  # no duplicates


def test_queries_use_profile_and_geography(settings, sample_cv_path) -> None:
    _, plan = _plan(settings, sample_cv_path, budget=80)
    texts = " || ".join(q.text for q in plan.all())
    assert '"Fullstack Developer"' in texts and '"Backend Developer"' in texts
    assert "site:linkedin.com/posts" in texts
    assert "site:jobstreet.co.id" in texts
    assert "lowongan" in texts  # Indonesian localisation
    assert "remote" in texts
    assert any(q.country_code == "ID" for q in plan.all())
    assert any(q.country is None for q in plan.all())  # worldwide/remote queries
    portal_names = {q.purpose for q in plan.queries[SourceType.JOB_PORTAL]}
    assert len(portal_names) >= 5  # many platforms even with limited budget
    assert all(q.freshness_days == 7 for q in plan.all())
    assert "node.js" in plan.keywords or "fullstack" in plan.keywords


def test_selected_sources_only(settings, sample_cv_path) -> None:
    _, plan = _plan(settings, sample_cv_path, budget=20, source_categories=["social_media"])
    assert plan.queries.get(SourceType.SOCIAL_MEDIA)
    assert not plan.queries.get(SourceType.JOB_PORTAL)


def test_english_only_has_no_indonesian_phrases(settings, sample_cv_path) -> None:
    _, plan = _plan(settings, sample_cv_path, budget=80, languages=["en"])
    assert not any(q.language == "id" for q in plan.all())


def test_zero_budget(settings, sample_cv_path) -> None:
    _, plan = _plan(settings, sample_cv_path, budget=0)
    assert plan.all() == []
    assert plan.keywords  # feeds still get keywords


def test_preferences_parsing() -> None:
    p = parse_preferences("Prefer software engineering jobs. Avoid banking companies.\nInclude startup opportunities")
    assert "banking" in p.negative
    assert any("startup" in t for t in p.positive)
