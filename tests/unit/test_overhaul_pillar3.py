"""Tests for Pillar 3 (HN monthly window, email query generation, country rotation, WWR & Working Nomads, SEA ATS)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from src.core.models import SearchConfig, SourceRunReport, SourceType
from src.cv.models import CandidateProfile
from src.discovery.query_generator import QueryGenerator
from src.discovery.source_registry import SourceRegistry
from src.extraction.job_normalizer import JobNormalizer
from src.intelligence.relevance import Prefilter
from src.sources.ats_platforms.ats import GreenhouseSource
from src.sources.base import DiscoveryContext
from src.sources.communities.hackernews import HackerNewsHiringSource
from src.sources.job_portals.remote_feeds import WorkingNomadsSource
from src.sources.job_portals.rss_feeds import WeWorkRemotelySource
from src.utils.http_client import SafeHttpClient


def _ctx(settings, handler, *, config: SearchConfig | None = None, profile: CandidateProfile | None = None):
    prof = profile or CandidateProfile(
        current_title="Backend Engineer",
        target_positions=["Backend Engineer", "Fullstack Developer"],
        role_families=["backend", "fullstack", "software_engineering"],
        skills=["Python", "Node.js", "PostgreSQL"],
    )
    cfg = config or SearchConfig(
        geography_mode="indonesia_worldwide",
        countries=["Indonesia"],
        include_worldwide=True,
        publication_window_days=7,
    )

    async def fake_resolver(_h: str) -> list[str]:
        return ["93.184.216.34"]

    http = SafeHttpClient(
        user_agent="TestBot/1.0",
        transport=httpx.MockTransport(handler),
        resolver=fake_resolver,
        default_host_interval=0.0,
    )
    plan = QueryGenerator(settings).generate(prof, cfg, budget=30)
    return DiscoveryContext(
        settings=settings,
        http=http,
        profile=prof,
        config=cfg,
        plan=plan,
        prefilter=Prefilter(settings, prof, cfg),
    )


def test_query_generator_activates_email_queries_phrases_and_country_rotation(settings) -> None:
    prof = CandidateProfile(
        current_title="Backend Developer",
        target_positions=["Backend Developer", "Fullstack Developer", "Node.js Developer"],
        role_families=["backend", "fullstack"],
        skills=["Node.js", "PostgreSQL"],
        frameworks=["Express"],
        programming_languages=["TypeScript"],
    )
    cfg = SearchConfig(
        geography_mode="indonesia_worldwide",
        countries=["Indonesia"],
        include_worldwide=True,
        languages=["en", "id"],
    )
    plan = QueryGenerator(settings).generate(prof, cfg, budget=80)
    gen_qs = plan.queries[SourceType.SEARCH_ENGINE]
    assert any("send your cv" in q.text.lower() or "kirim cv" in q.text.lower() for q in gen_qs)

    social_qs = plan.queries[SourceType.SOCIAL_MEDIA]
    # Should rotate beyond the first 3 phrases and beyond the first 3 targets
    assert any("send your cv" in q.text.lower() or "open position" in q.text.lower() or "join our team" in q.text.lower()
               for q in social_qs)
    social_countries = {q.country for q in social_qs}
    assert len(social_countries) >= 4

    ats_qs = plan.queries[SourceType.ATS]
    assert len({q.country for q in ats_qs}) >= 4


def test_hackernews_extends_monthly_window_for_7_day_setting(settings) -> None:
    twenty_days_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        if "search_by_date" in str(request.url):
            return httpx.Response(200, json={"hits": [{"objectID": "999", "title": "Ask HN: Who is hiring? (Nov 2026)"}]})
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "1001",
                        "parent_id": 999,
                        "created_at": twenty_days_ago,
                        "comment_text": "Acme Labs | Backend Engineer | REMOTE | Python, PostgreSQL. "
                                        + ("details " * 1000)
                                        + " Apply via email: jobs@acmelabs.io",
                    }
                ]
            },
        )

    async def run() -> None:
        ctx = _ctx(settings, handler, config=SearchConfig(publication_window_days=7))
        try:
            src = HackerNewsHiringSource(settings)
            report = SourceRunReport(name=src.name, category=src.category.value)
            items = await src.run(ctx, report)
            assert len(items) == 1
            assert any("extended" in n.lower() for n in report.notes)
            # Email at the end of a long comment is preserved before normalization!
            job = JobNormalizer(settings).normalise(items[0])
            assert "jobs@acmelabs.io" in job.application_emails
        finally:
            await ctx.http.aclose()

    asyncio.run(run())


def test_weworkremotely_rss_source_parses_company_role_and_email(settings) -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>WWR</title>
      <item>
        <title>Nusantara Cloud: Senior Backend Engineer</title>
        <region>Anywhere in the World</region>
        <category>Back-End Programming</category>
        <type>Full-Time</type>
        <pubDate>Tue, 24 Nov 2026 08:00:00 +0000</pubDate>
        <guid>https://weworkremotely.com/remote-jobs/nusantara-backend</guid>
        <link>https://weworkremotely.com/remote-jobs/nusantara-backend</link>
        <description>&lt;p&gt;We build APIs in Python and PostgreSQL. Send CV to hiring@nusantaracloud.io&lt;/p&gt;</description>
      </item>
    </channel></rss>"""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=rss)

    async def run() -> None:
        ctx = _ctx(settings, handler, config=SearchConfig(publication_window_days=None))
        try:
            src = WeWorkRemotelySource(settings)
            report = SourceRunReport(name=src.name, category=src.category.value)
            items = await src.run(ctx, report)
            assert len(items) == 1
            assert items[0].company == "Nusantara Cloud"
            assert items[0].title == "Senior Backend Engineer"
            assert items[0].work_arrangement == "remote"
            job = JobNormalizer(settings).normalise(items[0])
            assert "hiring@nusantaracloud.io" in job.application_emails
        finally:
            await ctx.http.aclose()

    asyncio.run(run())


def test_working_nomads_source_parses_exposed_jobs(settings) -> None:
    payload = [
        {
            "url": "https://www.workingnomads.com/jobs/backend-dev-123",
            "title": "Backend Engineer (Python)",
            "description": "<p>Join our distributed team building Python & PostgreSQL services.</p>",
            "company_name": "RemoteCo",
            "category_name": "Development",
            "tags": "python,postgresql,backend",
            "location": "Anywhere",
            "pub_date": datetime.now(timezone.utc).isoformat(),
        }
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run() -> None:
        ctx = _ctx(settings, handler, config=SearchConfig(publication_window_days=7))
        try:
            src = WorkingNomadsSource(settings)
            report = SourceRunReport(name=src.name, category=src.category.value)
            items = await src.run(ctx, report)
            assert len(items) == 1
            assert items[0].company == "RemoteCo"
            assert "python" in [t.lower() for t in items[0].tags]
        finally:
            await ctx.http.aclose()

    asyncio.run(run())


def test_sea_ats_boards_prioritized_for_indonesia_and_registry_loads_new_sources(settings) -> None:
    ctx = _ctx(settings, lambda _r: httpx.Response(200, json={"jobs": []}),
               config=SearchConfig(geography_mode="indonesia", countries=["Indonesia"], include_worldwide=False))
    try:
        boards, _ = GreenhouseSource(settings).boards(ctx)
        assert {"xendit", "flip", "kargo"} <= set(b.lower() for b in boards[:6])
    finally:
        asyncio.run(ctx.http.aclose())

    names = {s.name for s in SourceRegistry(settings).sources()}
    assert "We Work Remotely" in names
    assert "Working Nomads" in names
