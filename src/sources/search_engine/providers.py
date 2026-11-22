"""Web search provider abstraction.

Implemented provider:
* **Brave Search API** - ``GET https://api.search.brave.com/res/v1/web/search``
  with header ``X-Subscription-Token``; parameters ``q``, ``count`` (max 20),
  ``offset`` (0-9), ``country`` (2-letter), ``freshness`` (pd/pw/pm/py or
  ``YYYY-MM-DDtoYYYY-MM-DD``). Response ``web.results[]`` (title, url,
  description, optional age/page_age/extra_snippets) and
  ``query.more_results_available``. Source: Brave Search API documentation.

Not implemented on purpose: Google Custom Search JSON API (closed to new
customers; shuts down 2027-01-01) and Bing Web Search API (retired 2025).
Add another provider by subclassing :class:`SearchProvider` and registering it in
``PROVIDERS``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from src.core.exceptions import AuthenticationError, RateLimitError, SourceError
from src.core.models import QuerySpec
from src.utils.date_time import parse_datetime, utc_now
from src.utils.http_client import SafeHttpClient
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    description: str
    provider: str
    age_text: str | None = None
    page_age: str | None = None
    extra_snippets: list[str] = field(default_factory=list)
    rank: int = 0

    @property
    def published_hint(self):  # -> datetime | None
        """Date the search engine associates with the page (page_age preferred over relative age)."""
        return parse_datetime(self.page_age) or parse_datetime(self.age_text)


class BudgetExhausted(SourceError):
    def __init__(self) -> None:
        super().__init__("Search query budget exhausted (budget.max_search_engine_queries)", permanent=True)


class SearchProvider(ABC):
    name = "base"

    def __init__(self, http: SafeHttpClient, api_key: str) -> None:
        self.http = http
        self.api_key = api_key

    @abstractmethod
    async def search(self, query: QuerySpec, *, count: int, page: int) -> tuple[list[SearchHit], bool]:
        """Return (hits, more_results_available)."""


class BraveSearchProvider(SearchProvider):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    @staticmethod
    def freshness(days: int | None) -> str | None:
        if not days:
            return None
        end = utc_now().date()
        start = end - timedelta(days=int(days))
        return f"{start.isoformat()}to{end.isoformat()}"

    def build_params(self, query: QuerySpec, count: int, page: int, include_country: bool = True) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query.text, "count": max(1, min(count, 20)), "offset": max(0, min(page, 9))}
        if include_country and query.country_code:
            params["country"] = query.country_code
        fresh = self.freshness(query.freshness_days)
        if fresh:
            params["freshness"] = fresh
        return params

    async def search(self, query: QuerySpec, *, count: int, page: int) -> tuple[list[SearchHit], bool]:
        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        params = self.build_params(query, count, page)
        res = await self.http.get(self.endpoint, params=params, headers=headers, respect_robots=False)
        if res.status == 422 and "country" in params:
            # Country code not supported by the provider: retry once without it.
            params = self.build_params(query, count, page, include_country=False)
            res = await self.http.get(self.endpoint, params=params, headers=headers, respect_robots=False)
        if res.status in (401, 403):
            raise AuthenticationError(f"Brave Search rejected the API key (HTTP {res.status})")
        if res.status == 429:
            raise RateLimitError("Brave Search rate limit / quota reached (HTTP 429)", permanent=True)
        if not res.ok:
            raise SourceError(f"Brave Search error: {res.error}", permanent=res.status in (400, 404, 422))
        try:
            data = res.json()
        except ValueError as exc:
            raise SourceError("Brave Search returned invalid JSON") from exc
        return self.parse(data)

    def parse(self, data: dict[str, Any]) -> tuple[list[SearchHit], bool]:
        hits: list[SearchHit] = []
        for i, r in enumerate(((data.get("web") or {}).get("results")) or []):
            url = r.get("url")
            if not url:
                continue
            hits.append(SearchHit(
                title=str(r.get("title") or ""),
                url=str(url),
                description=str(r.get("description") or ""),
                provider=self.name,
                age_text=r.get("age"),
                page_age=r.get("page_age"),
                extra_snippets=[str(s) for s in (r.get("extra_snippets") or [])][:5],
                rank=i,
            ))
        more = bool((data.get("query") or {}).get("more_results_available"))
        return hits, more


PROVIDERS: dict[str, type[SearchProvider]] = {"brave": BraveSearchProvider}


class SearchService:
    """Primary/secondary provider orchestration with a global per-run query budget."""

    def __init__(self, providers: list[SearchProvider], *, max_queries: int, results_per_query: int,
                 max_pages: int) -> None:
        self.providers = providers
        self.max_queries = max_queries
        self.results_per_query = results_per_query
        self.max_pages = max(1, max_pages)
        self.used = 0
        self._dead: set[str] = set()
        self._lock = asyncio.Lock()
        self.errors: list[str] = []

    @classmethod
    def from_settings(cls, settings: Any, http: SafeHttpClient) -> "SearchService | None":
        creds = settings.credentials
        providers: list[SearchProvider] = []
        for name, key in ((creds.search_provider, creds.search_api_key),
                          (creds.secondary_search_provider, creds.secondary_search_api_key)):
            if name and key:
                cls_ = PROVIDERS.get(name.lower())
                if cls_ is None:
                    log_event(log, 30, "search.unsupported_provider", provider=name)
                    continue
                providers.append(cls_(http, key))
        if not providers:
            return None
        return cls(providers, max_queries=int(settings.get("budget.max_search_engine_queries", 40)),
                   results_per_query=int(settings.get("budget.results_per_query", 20)),
                   max_pages=int(settings.get("budget.max_pages_per_query", 1)))

    @property
    def available(self) -> bool:
        return any(p.name not in self._dead for p in self.providers)

    @property
    def remaining(self) -> int:
        return max(0, self.max_queries - self.used)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self.providers]

    async def _reserve(self) -> None:
        async with self._lock:
            if self.used >= self.max_queries:
                raise BudgetExhausted()
            self.used += 1

    async def search(self, query: QuerySpec) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for page in range(self.max_pages):
            page_hits, more = await self._search_page(query, page)
            hits.extend(page_hits)
            if not more:
                break
        return hits

    async def _search_page(self, query: QuerySpec, page: int) -> tuple[list[SearchHit], bool]:
        last_exc: SourceError | None = None
        for provider in self.providers:
            if provider.name in self._dead:
                continue
            await self._reserve()
            try:
                return await provider.search(query, count=self.results_per_query, page=page)
            except (AuthenticationError, RateLimitError) as exc:
                self._dead.add(provider.name)
                self.errors.append(str(exc))
                last_exc = exc
            except SourceError as exc:
                last_exc = exc
                if not exc.permanent:
                    self.errors.append(str(exc))
                    continue
                raise
        if last_exc:
            raise last_exc
        raise SourceError("No search provider available", permanent=True)
