"""Runs all applicable sources with per-source error isolation.

Stage 1: search-provider sources, public feeds and communities run concurrently.
Stage 2: late-stage sources (ATS boards) run afterwards so boards discovered in
stage 1 search results are crawled too. Each source's results are handed to
``on_results`` immediately so partial results survive failures/interruption.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from src.core.models import RawDiscovery, SourceRunReport
from src.discovery.source_registry import RegisteredSource, SourceRegistry
from src.sources.base import BaseJobSource, DiscoveryContext
from src.utils.date_time import iso, utc_now
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)

ResultsCallback = Callable[[BaseJobSource, list[RawDiscovery]], Awaitable[None] | None]


class DiscoveryManager:
    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry
        self.reports: dict[str, SourceRunReport] = {}

    def plan_sources(self, ctx: DiscoveryContext) -> list[tuple[RegisteredSource, SourceRunReport, bool]]:
        selected = set(ctx.config.source_categories)
        planned = []
        for entry in self.registry.entries:
            src = entry.source
            rep = SourceRunReport(name=src.name, category=src.category.value)
            runnable = False
            if not entry.enabled:
                rep.status = "disabled"
                rep.notes.append(entry.disabled_reason)
            elif src.category.value not in selected:
                rep.status = "not_selected"
                rep.notes.append("Source category not selected for this search.")
            else:
                ok, why = src.is_configured(ctx)
                if not ok:
                    rep.status = "skipped_no_credentials"
                    rep.notes.append(why)
                else:
                    runnable = True
            self.reports[src.name] = rep
            planned.append((entry, rep, runnable))
        return planned

    async def _run_one(self, src: BaseJobSource, rep: SourceRunReport, ctx: DiscoveryContext,
                       on_results: ResultsCallback | None) -> list[RawDiscovery]:
        rep.status = "running"
        rep.started_at = iso(utc_now())
        ctx.emit("source_start", {"source": src.name, "category": src.category.value})
        results: list[RawDiscovery] = []
        try:
            results = await src.run(ctx, rep)
            rep.results = len(results)
            if ctx.cancelled.is_set():
                rep.status = "interrupted"
            elif rep.errors and not results:
                rep.status = "failed"
            elif rep.errors:
                rep.status = "partial"
            else:
                rep.status = "success" if results else "no_results"
        except asyncio.CancelledError:
            rep.status = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 - one source never stops the others
            rep.status = "failed"
            rep.errors.append(f"{type(exc).__name__}: {exc}")
            log_event(log, 40, "source.failed", source=src.name, error=repr(exc))
        finally:
            rep.finished_at = iso(utc_now())
            ctx.emit("source_done", {"source": src.name, "status": rep.status, "results": rep.results})
        if results and on_results:
            maybe = on_results(src, results)
            if asyncio.iscoroutine(maybe):
                await maybe
        return results

    async def run(self, ctx: DiscoveryContext, on_results: ResultsCallback | None = None) -> list[RawDiscovery]:
        planned = self.plan_sources(ctx)
        early = [(e.source, r) for e, r, ok in planned if ok and not e.source.late_stage]
        late = [(e.source, r) for e, r, ok in planned if ok and e.source.late_stage]
        all_results: list[RawDiscovery] = []
        for stage in (early, late):
            if not stage or ctx.cancelled.is_set():
                for src, rep in stage:
                    rep.status = "interrupted"
                    rep.notes.append("Not started: search interrupted.")
                continue
            outputs = await asyncio.gather(*(self._run_one(s, r, ctx, on_results) for s, r in stage),
                                           return_exceptions=True)
            for out in outputs:
                if isinstance(out, BaseException):
                    if isinstance(out, asyncio.CancelledError):
                        raise out
                    continue
                all_results.extend(out)
        return all_results
