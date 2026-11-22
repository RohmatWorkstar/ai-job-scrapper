"""Public communities (Reddit, Dev.to, Hacker News, Kaskus) via indexed pages."""

from __future__ import annotations

from src.core.models import SourceType
from src.sources.search_engine.web_search import SearchBackedSource


class IndexedCommunitiesSource(SearchBackedSource):
    name = "Communities (indexed)"
    category = SourceType.COMMUNITY
    description = "Hiring threads in public communities found via site: queries"
