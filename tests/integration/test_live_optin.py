"""Real-network checks. Skipped unless RUN_LIVE_TESTS=1."""

from __future__ import annotations

import asyncio

import pytest

from src.core.models import SearchConfig, SourceRunReport
from src.core.orchestrator import build_http
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.discovery.query_generator import QueryGenerator
from src.intelligence.relevance import Prefilter
from src.sources.ats_platforms.ats import GreenhouseSource
from src.sources.base import DiscoveryContext
from tests.conftest import live_enabled

pytestmark = [pytest.mark.live, pytest.mark.skipif(not live_enabled(), reason="set RUN_LIVE_TESTS=1 to enable")]


def test_live_greenhouse_board(settings, sample_cv_path) -> None:
    settings.profiles["ats_seed_boards"] = {"greenhouse": ["gitlab"]}
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    config = SearchConfig(publication_window_days=None)

    async def go():
        http = build_http(settings)
        ctx = DiscoveryContext(settings=settings, http=http, profile=profile, config=config,
                               plan=QueryGenerator(settings).generate(profile, config, budget=0),
                               prefilter=Prefilter(settings, profile, config))
        rep = SourceRunReport("Greenhouse", "ats_platform")
        res = await GreenhouseSource(settings).run(ctx, rep)
        await http.aclose()
        return res, rep

    res, rep = asyncio.run(go())
    assert rep.items_scanned > 0 and not rep.errors
    assert all(d.source_url.startswith("https://") for d in res)
