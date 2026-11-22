from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.ai.gemini_provider import GeminiProvider, choose_model, extract_json
from src.ai.provider_manager import AIManager
from src.core.exceptions import AIProviderError, AuthenticationError
from src.core.models import QuerySpec, SearchConfig, SourceRunReport, SourceType
from src.core.orchestrator import build_http
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.discovery.discovery_manager import DiscoveryManager
from src.discovery.query_generator import QueryGenerator
from src.discovery.source_registry import SourceRegistry
from src.intelligence.relevance import Prefilter
from src.sources.ats_platforms.ats import GreenhouseSource, LeverSource
from src.sources.base import DiscoveryContext
from src.sources.communities.hackernews import parse_header
from src.sources.job_portals.remote_feeds import JobicySource
from src.sources.search_engine.providers import BraveSearchProvider, SearchService
from src.sources.search_engine.web_search import detect_ats
from tests.conftest import fixture_json, public_resolver, with_credentials


def _ctx(settings, router, sample_cv_path, search=None, **cfg) -> DiscoveryContext:
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    config = SearchConfig(publication_window_days=None, target_positions=["Backend Developer", "Fullstack Developer"], **cfg)
    http = build_http(settings, transport=router.transport(), resolver=public_resolver)
    plan = QueryGenerator(settings).generate(profile, config, budget=6)
    return DiscoveryContext(settings=settings, http=http, profile=profile, config=config, plan=plan,
                            prefilter=Prefilter(settings, profile, config), search=search)


# ------------------------------------------------------------- brave
def test_brave_request_and_parse(settings, router) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        seen["token"] = req.headers.get("X-Subscription-Token")
        return httpx.Response(200, json=fixture_json("brave_search.json"))

    router.add("api.search.brave.com", "/res/v1/web/search", handler)

    async def go():
        http = build_http(settings, transport=router.transport(), resolver=public_resolver)
        p = BraveSearchProvider(http, "test-key")
        q = QuerySpec(text='"Backend Developer" hiring', category=SourceType.SEARCH_ENGINE, purpose="general",
                      country="Indonesia", country_code="ID", freshness_days=7)
        hits, more = await p.search(q, count=20, page=0)
        await http.aclose()
        return hits, more

    hits, more = asyncio.run(go())
    assert seen["token"] == "test-key" and seen["country"] == "ID" and seen["count"] == "20"
    assert "to" in seen["freshness"]  # YYYY-MM-DDtoYYYY-MM-DD
    assert len(hits) == 4 and more is False
    assert hits[0].published_hint is not None


def test_brave_auth_error_is_permanent(settings, router) -> None:
    router.add("api.search.brave.com", "/", httpx.Response(401, json={"error": "unauthorized"}))

    async def go():
        http = build_http(settings, transport=router.transport(), resolver=public_resolver)
        svc = SearchService([BraveSearchProvider(http, "bad")], max_queries=5, results_per_query=20, max_pages=1)
        q = QuerySpec(text="x", category=SourceType.SEARCH_ENGINE, purpose="general")
        try:
            with pytest.raises(AuthenticationError):
                await svc.search(q)
            assert not svc.available
        finally:
            await http.aclose()

    asyncio.run(go())
    assert router.hits("api.search.brave.com") == 1  # never retried


def test_ats_detection_from_urls() -> None:
    assert detect_ats("https://job-boards.greenhouse.io/contohtek/jobs/4455667") == ("greenhouse", "contohtek")
    assert detect_ats("https://jobs.lever.co/fixtureco/abc") == ("lever", "fixtureco")
    assert detect_ats("https://jobs.ashbyhq.com/openai/xyz") == ("ashby", "openai")
    assert detect_ats("https://example.com/jobs") is None


# ------------------------------------------------------------- ATS + feeds
def test_greenhouse_board_prefilter_and_detail(settings, router, sample_cv_path) -> None:
    router.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs/4455667", fixture_json("greenhouse_job_detail.json"))
    router.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs", fixture_json("greenhouse_jobs.json"))
    settings.profiles["ats_seed_boards"] = {"greenhouse": ["contohtek"]}

    async def go():
        ctx = _ctx(settings, router, sample_cv_path)
        rep = SourceRunReport("Greenhouse", "ats_platform")
        res = await GreenhouseSource(settings).run(ctx, rep)
        await ctx.http.aclose()
        return res, rep

    res, rep = asyncio.run(go())
    titles = {d.title for d in res}
    assert "Backend Engineer" in titles and "Frontend Engineer" in titles
    assert "Senior Account Executive" not in titles  # unrelated job family filtered (and counted)
    be = next(d for d in res if d.title == "Backend Engineer")
    assert be.official_channel and be.listed_in_live_feed and be.company == "Contoh Teknologi"
    assert "PostgreSQL" in be.text and be.external_id == "greenhouse:contohtek:4455667"
    assert rep.items_scanned == 3 and "unrelated" in rep.notes[-1]


def test_lever_parsing(settings, router, sample_cv_path) -> None:
    router.add("api.lever.co", "/v0/postings/fixtureco", fixture_json("lever_postings.json"))
    settings.profiles["ats_seed_boards"] = {"lever": ["fixtureco"]}

    async def go():
        ctx = _ctx(settings, router, sample_cv_path)
        res = await LeverSource(settings).run(ctx, SourceRunReport("Lever", "ats_platform"))
        await ctx.http.aclose()
        return res

    (d,) = asyncio.run(go())
    assert d.work_arrangement == "remote" and d.salary_currency == "USD" and d.salary_min == 30000
    assert d.application_url.endswith("/apply") and d.source_url != d.application_url


def test_feed_failure_isolated(settings, router, sample_cv_path) -> None:
    router.add("jobicy.com", "/", httpx.Response(500, text="boom"))

    async def go():
        ctx = _ctx(settings, router, sample_cv_path)
        rep = SourceRunReport("Jobicy", "job_portal")
        res = await JobicySource(settings).run(ctx, rep)
        await ctx.http.aclose()
        return res, rep

    res, rep = asyncio.run(go())
    assert res == [] and rep.errors


def test_discovery_manager_isolates_failures_and_reports_status(settings, router, sample_cv_path) -> None:
    router.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs/4455667", fixture_json("greenhouse_job_detail.json"))
    router.add("boards-api.greenhouse.io", "/v1/boards/contohtek/jobs", fixture_json("greenhouse_jobs.json"))
    router.add("boards-api.greenhouse.io", "/v1/boards/brokenco", httpx.Response(404, json={}))
    settings.profiles["ats_seed_boards"] = {"greenhouse": ["brokenco", "contohtek"]}
    settings.raw["discovery"]["connectors"] = [
        "src.sources.ats_platforms.ats:GreenhouseSource",
        "src.sources.job_portals.remote_feeds:JobicySource",
        "src.sources.search_engine.web_search:WebSearchSource",
    ]

    async def go():
        ctx = _ctx(settings, router, sample_cv_path)
        mgr = DiscoveryManager(SourceRegistry(settings))
        out = await mgr.run(ctx)
        await ctx.http.aclose()
        return out, mgr.reports

    out, reports = asyncio.run(go())
    assert any(d.title == "Backend Engineer" for d in out)
    assert reports["Greenhouse"].status == "partial"  # one board failed, other succeeded
    assert reports["Jobicy"].status == "failed"
    assert reports["Web Search"].status == "skipped_no_credentials"


def test_hn_header_parsing() -> None:
    company, role, loc = parse_header("Fixture Labs | Senior Backend Engineer | Remote (APAC) | Full-time\nWe build...")
    assert company == "Fixture Labs" and role == "Senior Backend Engineer" and "Remote" in loc


# ------------------------------------------------------------- AI provider
def test_choose_model_prefers_newest_stable() -> None:
    ids = [m["name"].removeprefix("models/") for m in fixture_json("gemini_models.json")["models"]]
    assert choose_model(ids, "flash") == "gemini-3.8-flash"
    assert choose_model(ids, "flash-lite") == "gemini-3.5-flash-lite"
    assert choose_model(["gemini-flash-latest"], "flash") == "gemini-flash-latest"
    assert choose_model(["text-embedding-004"], "flash") is None


def test_extract_json_handles_fences() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here: {"a": [1,2]} done') == {"a": [1, 2]}
    with pytest.raises(AIProviderError):
        extract_json("no json here")


def test_gemini_request_response_cycle(settings, router) -> None:
    captured = {}

    def gen(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["key"] = req.headers.get("x-goog-api-key")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]},
                                                         "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}})

    router.add("generativelanguage.googleapis.com", "/v1beta/models/gemini-3.8-flash:generateContent", gen)
    router.add("generativelanguage.googleapis.com", "/v1beta/models", fixture_json("gemini_models.json"))
    s = with_credentials(settings, gemini_api_key="test-gemini-key")

    async def go():
        http = build_http(s, transport=router.transport(), resolver=public_resolver)
        ai = AIManager.from_settings(s, http)
        first = await ai.ask_json("t", "prompt one")
        second = await ai.ask_json("t", "prompt one")  # served from cache
        await http.aclose()
        return ai, first, second

    ai, first, second = asyncio.run(go())
    assert first == {"ok": True} == second
    assert ai.model == "gemini-3.8-flash" and ai.calls == 1 and ai.cache_hits == 1
    assert captured["key"] == "test-gemini-key"
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "UNTRUSTED" in captured["body"]["systemInstruction"]["parts"][0]["text"]


def test_gemini_invalid_key_disables_ai_gracefully(settings, router) -> None:
    router.add("generativelanguage.googleapis.com", "/", httpx.Response(400, json={"error": {"message": "API key not valid"}}))
    s = with_credentials(settings, gemini_api_key="bad", ai_model="gemini-3.8-flash")

    async def go():
        http = build_http(s, transport=router.transport(), resolver=public_resolver)
        ai = AIManager.from_settings(s, http)
        r1 = await ai.ask_json("t", "p1")
        r2 = await ai.ask_json("t", "p2")
        await http.aclose()
        return ai, r1, r2

    ai, r1, r2 = asyncio.run(go())
    assert r1 is None and r2 is None and not ai.available
    assert router.hits("generativelanguage.googleapis.com") == 1  # permanent error: no further calls


def test_gemini_blocked_prompt(settings, router) -> None:
    router.add("generativelanguage.googleapis.com", "/", {"promptFeedback": {"blockReason": "SAFETY"}})

    async def go():
        http = build_http(settings, transport=router.transport(), resolver=public_resolver)
        p = GeminiProvider(http, "k", model="gemini-3.8-flash")
        try:
            with pytest.raises(AIProviderError):
                await p.generate_json("sys", "prompt")
        finally:
            await http.aclose()

    asyncio.run(go())


def test_ai_budget_enforced(settings, router) -> None:
    router.add("generativelanguage.googleapis.com", "/", {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})
    s = with_credentials(settings, gemini_api_key="k", ai_model="gemini-3.8-flash")
    s.raw["ai"]["max_calls_per_run"] = 2
    s.raw["ai"]["cache_responses"] = False

    async def go():
        http = build_http(s, transport=router.transport(), resolver=public_resolver)
        ai = AIManager.from_settings(s, http)
        results = [await ai.ask_json("t", f"p{i}") for i in range(4)]
        await http.aclose()
        return ai, results

    ai, results = asyncio.run(go())
    assert results[:2] == [{}, {}] and results[2:] == [None, None] and ai.calls == 2
