"""End-to-end pipeline with fixture data served through a mock transport (no network)."""

from __future__ import annotations

import asyncio
import json

import httpx
from openpyxl import load_workbook

from src.core.models import SearchConfig
from src.core.orchestrator import Orchestrator
from src.core.pipeline import PipelineState
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from tests.conftest import Router, fixture_json, fixture_text, public_resolver, with_credentials


def _router() -> Router:
    r = Router()
    r.add("api.search.brave.com", "/res/v1/web/search", fixture_json("brave_search.json"))
    r.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs/4455667", fixture_json("greenhouse_job_detail.json"))
    r.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs", fixture_json("greenhouse_jobs.json"))
    r.add("api.lever.co", "/v0/postings/fixtureco", fixture_json("lever_postings.json"))
    r.add("careers.example-corp.test", "/robots.txt", httpx.Response(404))
    r.add("careers.example-corp.test", "/jobs/fullstack-developer", fixture_text("career_page.html"))
    r.add("blog.example-news.test", "/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n",
                                                                     headers={"content-type": "text/plain"}))
    r.add("job-boards.greenhouse.io", "/robots.txt", httpx.Response(404))
    r.add("job-boards.greenhouse.io", "/contohtek/jobs/4455667", fixture_text("greenhouse_job_page.html"))
    r.add("jobicy.com", "/", httpx.Response(503, text="maintenance"))  # failing source must not stop the run
    return r


def _settings(settings):
    s = with_credentials(settings, search_provider="brave", search_api_key="test-brave-key")
    s.profiles["ats_seed_boards"] = {"greenhouse": ["contohtek"], "lever": ["fixtureco"]}
    s.raw["budget"]["max_search_engine_queries"] = 6
    s.raw["discovery"]["connectors"] = [
        "src.sources.search_engine.web_search:WebSearchSource",
        "src.sources.social_media.indexed_social:IndexedSocialMediaSource",
        "src.sources.job_portals.remote_feeds:JobicySource",
        "src.sources.ats_platforms.ats:GreenhouseSource",
        "src.sources.ats_platforms.ats:LeverSource",
        "src.sources.communities.reddit:RedditSource",
    ]
    return s


def test_full_pipeline_with_fixtures(settings, sample_cv_path) -> None:
    s = _settings(settings)
    router = _router()
    profile = CVAnalyzer(s).analyze(parse_cv(sample_cv_path))
    config = SearchConfig(target_positions=["Backend Developer", "Fullstack Developer"], publication_window_days=None)
    state = PipelineState()
    orch = Orchestrator(s, state=state, transport=router.transport(), resolver=public_resolver)
    try:
        result = asyncio.run(orch.run(profile, config))
    finally:
        orch.close()

    assert result.status in ("completed", "completed_with_errors")
    assert all(v in ("COMPLETED",) for v in state.stages.values()), state.stages
    data = result.data
    jobs = {j.job_title: j for j in data.jobs}

    # ATS posting found via Greenhouse API and via the search result -> merged into one entry
    be = jobs["Backend Engineer"]
    assert be.verification_status == "VERIFIED_OPEN"
    assert any("greenhouse" in s.source_url for s in be.additional_sources) or "greenhouse" in be.source_url
    assert data.duplicates_merged >= 1
    # Company careers page enriched from JSON-LD despite the prompt-injection text in its description
    fs = [j for j in data.jobs if j.company_name == "Example Corp"]
    assert fs and fs[0].verification_status in ("VERIFIED_OPEN", "LIKELY_OPEN")
    # Social post is kept but needs review; its URL is preserved
    social = [j for j in data.jobs if "linkedin.com/posts" in j.source_url]
    assert social and social[0].verification_status == "NEEDS_REVIEW"
    # Blog article (robots-disallowed, not a job) is kept for transparency but not relevant
    blog = [j for j in data.jobs if "example-news" in j.source_url]
    assert blog and blog[0].relevance_category == "NOT RELEVANT"
    # Sales role filtered from the ATS board; Lever remote role present with salary in original currency
    assert "Senior Account Executive" not in jobs
    assert jobs["Fullstack Developer"].salary_currency in ("USD", "IDR")

    statuses = {r.name: r.status for r in data.source_reports}
    assert statuses["Jobicy"] == "failed"
    assert statuses["Reddit (API)"] == "skipped_no_credentials"
    assert statuses["Greenhouse"] == "success"
    assert any(q.source_name == "Web Search" for q in data.queries)

    files = result.report_files
    for key in ("docx", "xlsx", "metadata", "raw"):
        assert files[key].exists(), key
    meta = json.loads(files["metadata"].read_text(encoding="utf-8"))
    assert meta["timezone"] == "Asia/Jakarta" and meta["counts"]["unique_opportunities"] == len(data.jobs)
    assert "test-brave-key" not in files["metadata"].read_text(encoding="utf-8")
    assert "test-brave-key" not in files["raw"].read_text(encoding="utf-8")
    wb = load_workbook(files["xlsx"])
    assert wb["All Opportunities"].max_row == len(data.jobs) + 1  # every opportunity present
    assert result.report_dir.name.startswith(data.fmt(data.started_at, False))


def test_report_regeneration_from_storage(settings, sample_cv_path) -> None:
    s = _settings(settings)
    router = _router()
    profile = CVAnalyzer(s).analyze(parse_cv(sample_cv_path))
    orch = Orchestrator(s, transport=router.transport(), resolver=public_resolver)
    try:
        pid = orch.repo.save_profile(profile)
        result = asyncio.run(orch.run(profile, SearchConfig(publication_window_days=None), profile_id=pid))
        folder, files = orch.regenerate_reports(result.session_id)
    finally:
        orch.close()
    assert files["docx"].exists() and files["xlsx"].exists()
    wb = load_workbook(files["xlsx"])
    assert wb["All Opportunities"].max_row == len(result.data.jobs) + 1


def test_interrupted_search_keeps_partial_results(settings, sample_cv_path) -> None:
    s = _settings(settings)
    router = _router()

    def slow_lever(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    router.routes.insert(0, ("api.lever.co", "/", slow_lever))
    profile = CVAnalyzer(s).analyze(parse_cv(sample_cv_path))
    orch = Orchestrator(s, transport=router.transport(), resolver=public_resolver)

    async def run_and_cancel():
        task = asyncio.create_task(orch.run(profile, SearchConfig(publication_window_days=None)))
        # Cancel once results have started arriving (simulates Ctrl+C mid-discovery).
        for _ in range(200):
            await asyncio.sleep(0.01)
            if orch.state.results_discovered > 0:
                break
        task.cancel()
        return await task

    try:
        result = asyncio.run(run_and_cancel())
    finally:
        orch.close()
    assert result.report_files.get("xlsx") is not None
    assert result.status in ("interrupted", "completed", "completed_with_errors")
    if result.status == "interrupted":
        assert any("interrupted" in l.lower() for l in result.data.limitations)
