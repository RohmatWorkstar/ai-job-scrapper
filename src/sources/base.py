"""Common interface for every discovery source connector.

To add a new source: subclass :class:`BaseJobSource`, implement ``search`` (and
optionally ``build_queries``/``run``/``health_check``), then list it under
``discovery.connectors`` in config/settings.yaml. The discovery engine needs no change.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from src.core.exceptions import AuthenticationError, SourceError
from src.core.models import ExecutedQuery, QuerySpec, RawDiscovery, SearchConfig, SourceRunReport, SourceType
from src.utils.date_time import iso, utc_now
from src.utils.logger import get_logger, log_event

if TYPE_CHECKING:  # pragma: no cover
    from src.core.configuration import Settings
    from src.cv.models import CandidateProfile
    from src.discovery.query_generator import QueryPlan
    from src.intelligence.relevance import Prefilter
    from src.sources.search_engine.providers import SearchService
    from src.utils.http_client import SafeHttpClient

log = get_logger(__name__)


@dataclass
class DiscoveryContext:
    settings: "Settings"
    http: "SafeHttpClient"
    profile: "CandidateProfile"
    config: SearchConfig
    plan: "QueryPlan"
    prefilter: "Prefilter"
    search: "SearchService | None" = None
    executed: list[ExecutedQuery] = field(default_factory=list)
    ats_tokens: dict[str, set[str]] = field(default_factory=dict)
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def record_query(self, source: str, category: SourceType, query: str, status: str, count: int,
                     error: str | None = None) -> None:
        self.executed.append(ExecutedQuery(source, category.value, query, status, count, iso(utc_now()) or "", error))
        self.emit("query", {"source": source, "category": category.value, "status": status, "count": count})

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, data)
            except Exception:  # noqa: BLE001 - UI callbacks must never break discovery
                pass

    def add_ats_token(self, platform: str, token: str) -> None:
        self.ats_tokens.setdefault(platform, set()).add(token)


class BaseJobSource(ABC):
    name: str = "base"
    category: SourceType = SourceType.OTHER
    description: str = ""
    #: True if this source consumes the search-engine provider (needs SEARCH_API_KEY)
    uses_search_provider: bool = False
    #: Run after other sources (e.g. ATS crawling that consumes discovered board tokens)
    late_stage: bool = False

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings

    # ----------------------------------------------------------- capability
    def is_configured(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if self.uses_search_provider and (ctx.search is None or not ctx.search.available):
            return False, "No search API configured (SEARCH_PROVIDER / SEARCH_API_KEY in .env)"
        return True, ""

    def build_queries(self, ctx: DiscoveryContext) -> list[QuerySpec]:
        return ctx.plan.for_source(self.category, self.name)

    @abstractmethod
    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:
        """Execute one query against this source."""

    async def extract(self, url: str, ctx: DiscoveryContext) -> RawDiscovery | None:
        """Fetch a single URL and extract a discovery from it (HTML/JSON-LD)."""
        from src.extraction.html_extractor import fetch_and_extract

        page = await fetch_and_extract(ctx.http, url, ctx.settings)
        if page is None or not page.ok:
            return None
        return page.to_discovery(self.category, self.name, url)

    async def health_check(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        ok, reason = self.is_configured(ctx)
        return ok, reason or "configured"

    # ------------------------------------------------------------------ run
    async def run(self, ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        results: list[RawDiscovery] = []
        for q in self.build_queries(ctx):
            if ctx.cancelled.is_set():
                report.notes.append("Interrupted before all queries ran.")
                break
            try:
                found = await self.search(q, ctx)
                report.queries_executed += 1
                results.extend(found)
                ctx.record_query(self.name, self.category, q.text, "ok" if found else "no_results", len(found))
            except AuthenticationError as exc:
                ctx.record_query(self.name, self.category, q.text, "auth_error", 0, str(exc))
                report.errors.append(str(exc))
                break  # never retry a permanently unauthorized source
            except SourceError as exc:
                ctx.record_query(self.name, self.category, q.text, "error", 0, str(exc))
                report.errors.append(str(exc))
                if exc.permanent:
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate unexpected connector bugs
                log_event(log, 40, "source.unexpected_error", source=self.name, error=repr(exc))
                ctx.record_query(self.name, self.category, q.text, "error", 0, f"Unexpected error: {type(exc).__name__}")
                report.errors.append(f"Unexpected error: {type(exc).__name__}: {exc}")
        return results


def http_error_to_exception(source: str, res: Any) -> SourceError:
    """Translate an HttpResult failure into the right SourceError subtype."""
    if res.status in (401, 403):
        return AuthenticationError(f"{source}: access denied (HTTP {res.status})")
    if res.status == 429:
        from src.core.exceptions import RateLimitError

        return RateLimitError(f"{source}: rate limited (HTTP 429)")
    if res.error_kind == "blocked_robots":
        return SourceError(f"{source}: blocked by robots.txt", permanent=True)
    permanent = res.status in (404, 410) or res.error_kind in {"unsafe_url", "content_type"}
    return SourceError(f"{source}: {res.error or 'HTTP ' + str(res.status)}", permanent=permanent)
