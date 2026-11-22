"""End-to-end research pipeline: queries -> discovery -> extraction -> verification ->
matching -> persistence -> reports. Partial results survive source failures and Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from src.ai.provider_manager import AIManager
from src.core.configuration import Settings
from src.core.models import ContentKind, JobOpportunity, RawDiscovery, SearchConfig, SourceType
from src.core.pipeline import COMPLETED, FAILED, INTERRUPTED, RUNNING, SKIPPED, PipelineState
from src.cv.models import CandidateProfile
from src.discovery.discovery_manager import DiscoveryManager
from src.discovery.query_generator import QueryGenerator, describe_plan
from src.discovery.source_registry import SourceRegistry
from src.extraction.content_parser import job_signal_strength
from src.extraction.html_extractor import fetch_and_extract
from src.extraction.job_normalizer import JobNormalizer, ai_extract, enrich_with_page
from src.intelligence.deduplication import deduplicate
from src.intelligence.matching import Matcher, ai_rerank
from src.intelligence.relevance import Prefilter
from src.intelligence.verification import Verifier
from src.reporting.docx_generator import DocxReportGenerator
from src.reporting.excel_generator import ExcelReportGenerator
from src.reporting.report_models import ReportData
from src.sources.base import DiscoveryContext
from src.sources.search_engine.providers import SearchService
from src.sources.search_engine.web_search import detect_ats
from src.storage.database import Database
from src.storage.history import SearchHistory
from src.storage.repository import Repository
from src.utils.date_time import folder_timestamp, utc_now
from src.utils.http_client import SafeHttpClient
from src.utils.logger import get_logger, log_event
from src.utils.validators import Resolver, domain_matches

log = get_logger(__name__)

FETCH_PRIORITY = {SourceType.ATS: 0, SourceType.COMPANY_CAREERS: 1, SourceType.JOB_PORTAL: 2,
                  SourceType.SEARCH_ENGINE: 3, SourceType.COMMUNITY: 8, SourceType.SOCIAL_MEDIA: 9, SourceType.OTHER: 5}


@dataclass
class RunResult:
    session_id: str
    status: str
    report_dir: Path | None
    report_files: dict[str, Path] = field(default_factory=dict)
    data: ReportData | None = None
    errors: list[str] = field(default_factory=list)


def build_http(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               resolver: Resolver | None = None, timeout: float | None = None) -> SafeHttpClient:
    return SafeHttpClient(
        user_agent=settings.user_agent,
        timeout=float(timeout or settings.get("performance.request_timeout_seconds", 30)),
        max_concurrency=int(settings.get("performance.max_concurrency", 5)),
        retry_attempts=int(settings.get("performance.retry_attempts", 2)),
        backoff_base=float(settings.get("performance.backoff_base_seconds", 1.5)),
        max_bytes=int(settings.get("performance.max_response_bytes", 4_000_000)),
        default_host_interval=float(settings.get("performance.default_host_interval_seconds", 1.0)),
        host_intervals=settings.get("performance.host_intervals", {}) or {},
        cache_dir=settings.cache_directory,
        cache_ttl_hours=settings.get("performance.cache_ttl_hours", {}) or {},
        transport=transport,
        resolver=resolver,
    )


def build_limitations(settings: Settings, search: SearchService | None, ai: AIManager, interrupted: bool) -> list[str]:
    lim = [
        "Coverage is limited to the sources listed in this report; the internet as a whole was not searched.",
        "LinkedIn, Facebook, Instagram, X/Twitter, Threads, Glassdoor and Indeed pages are never fetched directly "
        "(login walls / terms of service). Only their publicly indexed search snippets are used, so those items "
        "remain unverified until checked manually.",
        "Search-engine date filters refer to when pages were indexed; some older postings can appear.",
        "Remotive publishes its API data with a 24-hour delay (per its API terms).",
        "A page being reachable does not prove applications are still accepted; see each item's verification reason.",
        "Workday has no documented public job API; Workday postings are only found via search results.",
    ]
    if search is None:
        lim.insert(0, "No web search API key configured (SEARCH_API_KEY): general web, job portals, social media, "
                      "community and careers-page search were not performed. Only public job APIs, ATS boards and "
                      "Hacker News were searched.")
    else:
        lim.append(f"Paid web-search usage capped at {search.max_queries} queries per run "
                   f"(budget.max_search_engine_queries); {search.used} used.")
    if not ai.available or ai.calls == 0:
        lim.append("AI assistance " + ("was not used: " + ai.disabled_reason if ai.disabled_reason else
                                       "was not needed or produced no calls") +
                   ". Profile, extraction and matching used deterministic rules only.")
    if not settings.credentials.reddit_client_id:
        lim.append("Reddit API credentials not set: Reddit covered only through indexed pages (if search is configured).")
    if not settings.credentials.x_bearer_token:
        lim.append("X API token not set: X/Twitter covered only through indexed public posts (if search is configured).")
    if interrupted:
        lim.insert(0, "The search was interrupted by the user; results collected up to that point are included.")
    return lim


class Orchestrator:
    def __init__(self, settings: Settings, *, state: PipelineState | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None,
                 ai_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.state = state or PipelineState()
        self._transport = transport
        self._ai_transport = ai_transport
        self._resolver = resolver
        self.db = Database(settings.database_path)
        self.repo = Repository(self.db)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- helpers
    async def _fetch_pages(self, http: SafeHttpClient, raws: list[RawDiscovery], limit: int,
                           cancelled: asyncio.Event) -> int:
        no_fetch = self.settings.get("extraction.no_fetch_domains", []) or []
        candidates = [d for d in raws if not (d.structured and d.listed_in_live_feed)
                      and d.content_kind not in (ContentKind.SOCIAL_POST, ContentKind.COMMUNITY_POST)
                      and not domain_matches(d.source_url, no_fetch)]
        chosen_ids = {id(d) for d in candidates}
        for d in raws:
            if id(d) not in chosen_ids and not d.structured and domain_matches(d.source_url, no_fetch):
                d.raw["fetch"] = {"ok": False, "error_kind": "policy",
                                  "error": "not fetched: platform requires login / restricts automated access"}
        candidates.sort(key=lambda d: (FETCH_PRIORITY.get(d.source_type, 5), d.raw.get("rank", 0)))
        selected = candidates[:limit]
        self.state.pages_to_fetch = len(selected)
        sem = asyncio.Semaphore(int(self.settings.get("performance.max_concurrency", 5)))

        async def one(d: RawDiscovery) -> None:
            if cancelled.is_set():
                return
            async with sem:
                try:
                    page = await fetch_and_extract(http, d.source_url, self.settings)
                    enrich_with_page(d, page)
                except Exception as exc:  # noqa: BLE001 - one page never stops the stage
                    d.raw["fetch"] = {"ok": False, "error": f"{type(exc).__name__}", "error_kind": "network"}
                self.state.pages_fetched += 1
                self.state._notify()

        await asyncio.gather(*(one(d) for d in selected))
        return len(selected)

    def _normalise(self, raws: list[RawDiscovery]) -> list[JobOpportunity]:
        normalizer = JobNormalizer(self.settings)
        jobs: list[JobOpportunity] = []
        for d in raws:
            try:
                job = normalizer.normalise(d)
            except Exception as exc:  # noqa: BLE001 - malformed data from one source
                log_event(log, 30, "normalise.failed", url=d.source_url, error=repr(exc))
                continue
            job.signals["fetch"] = d.raw.get("fetch") or {}
            if d.raw.get("ai_not_a_job"):
                job.signals["not_a_job"] = True
            if not d.structured:
                signal = job_signal_strength(f"{d.title} {d.snippet} {d.text[:1500]}")
                job.signals["job_signal"] = round(signal, 2)
                if signal < 0.15 and d.content_kind != ContentKind.JOB_POSTING and not detect_ats(d.source_url):
                    job.signals["not_a_job"] = True
            jobs.append(job)
        return jobs

    # ----------------------------------------------------------------- main
    async def run(self, profile: CandidateProfile, config: SearchConfig, *, profile_id: int | None = None) -> RunResult:
        st = self.state
        started = utc_now()
        session_id = f"{folder_timestamp(started, self.settings.timezone)}-{uuid.uuid4().hex[:6]}"
        self.repo.create_session(session_id, profile_id, config)
        log_event(log, 20, "session.start", session=session_id)
        interrupted = False
        errors: list[str] = []
        collected: list[RawDiscovery] = []
        http = build_http(self.settings, transport=self._transport, resolver=self._resolver)
        ai_http = build_http(self.settings, transport=self._ai_transport or self._transport, resolver=self._resolver,
                             timeout=float(self.settings.get("ai.request_timeout_seconds", 90)))
        ai = AIManager.from_settings(self.settings, ai_http)
        search = SearchService.from_settings(self.settings, http)
        st.set("CV analysis", COMPLETED, profile.analysis_method)
        manager: DiscoveryManager | None = None
        ctx: DiscoveryContext | None = None
        try:
            # ---- 2. query generation
            st.set("Search query generation", RUNNING)
            plan = QueryGenerator(self.settings).generate(profile, config)
            plan_info = describe_plan(plan)
            st.set("Search query generation", COMPLETED,
                   f"{sum(plan_info['per_category'].values())} search queries planned, keywords: "
                   f"{', '.join(plan.keywords[:5])}")

            # ---- 3. discovery
            st.set("Multi-source discovery", RUNNING)
            prefilter = Prefilter(self.settings, profile, config)
            ctx = DiscoveryContext(settings=self.settings, http=http, profile=profile, config=config, plan=plan,
                                   prefilter=prefilter, search=search, on_event=st.on_event)
            manager = DiscoveryManager(SourceRegistry(self.settings))

            async def persist(src: Any, items: list[RawDiscovery]) -> None:
                collected.extend(items)
                st.add_results(len(items))
                try:
                    self.repo.save_raw(session_id, items)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Could not persist partial results: {exc}")

            try:
                await manager.run(ctx, on_results=persist)
                st.set("Multi-source discovery", COMPLETED, f"{len(collected)} results")
            except asyncio.CancelledError:
                interrupted = True
                ctx.cancelled.set()
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                st.set("Multi-source discovery", INTERRUPTED, f"{len(collected)} results kept")

            # ---- 4. extraction
            st.set("Information extraction", RUNNING)
            fetched = 0
            history = SearchHistory(self.repo)
            previous: list[JobOpportunity] = []
            if not interrupted:
                try:
                    if self.settings.get("extraction.fetch_pages_for_verification", True):
                        fetched = await self._fetch_pages(http, collected,
                                                          int(self.settings.get("budget.max_pages_to_fetch", 60)),
                                                          ctx.cancelled)
                    st.ai_calls = ai.calls
                    await ai_extract(collected, ai, int(self.settings.get("ai.extraction_batch_size", 8)))
                    st.ai_calls = ai.calls
                except asyncio.CancelledError:
                    interrupted = True
                    ctx.cancelled.set()
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
            jobs = self._normalise(collected)
            if config.include_previous_jobs and not interrupted:
                previous = history.previous_jobs(session_id)
                known = {j.job_id for j in jobs}
                previous = [p for p in previous if p.job_id not in known][:100]
                await self._recheck_previous(http, previous, ctx.cancelled)
                jobs.extend(previous)
            unique, merged = deduplicate(jobs)
            st.unique_opportunities = len(unique)
            st.set("Information extraction", INTERRUPTED if interrupted else COMPLETED,
                   f"{fetched} pages checked, {len(unique)} unique ({merged} duplicates merged)")

            # ---- 5. verification
            st.set("Verification", RUNNING)
            verifier = Verifier(self.settings)
            for j in unique:
                if j.from_history:
                    j.listed_in_live_feed = False
                verifier.apply(j)
            st.set("Verification", COMPLETED)

            # ---- 6. matching
            st.set("Job matching", RUNNING)
            matcher = Matcher(self.settings, profile, config, plan.preferences)
            for j in unique:
                matcher.apply(j)
            if not interrupted:
                try:
                    await ai_rerank(unique, matcher, ai, profile, config, self.settings)
                except asyncio.CancelledError:
                    interrupted = True
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
            for j in unique:  # freshness can mark stale postings
                if j.signals.get("stale") and "Posting is older than 60 days." not in j.warnings:
                    j.warnings.append("Posting is older than 60 days.")
            st.ai_calls = ai.calls
            st.set("Job matching", COMPLETED, f"AI: {ai.status_line()}")

            # ---- persistence
            new_count = history.mark_new([j for j in unique if not j.from_history])
            try:
                self.repo.save_opportunities(session_id, unique)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Could not save opportunities to history: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep whatever we have and still report
            log_event(log, 40, "pipeline.error", error=repr(exc))
            errors.append(f"Pipeline error: {type(exc).__name__}: {exc}")
            for stage, status in st.stages.items():
                if status == RUNNING:
                    st.set(stage, FAILED, str(exc)[:120])
            jobs = self._normalise(collected)
            unique, merged = deduplicate(jobs)
            new_count = 0
            plan = None  # type: ignore[assignment]

        # ---- 7. reports (always attempted)
        completed = utc_now()
        reports = list(manager.reports.values()) if manager else []
        queries = ctx.executed if ctx else []
        status = "interrupted" if interrupted else ("completed_with_errors" if errors else "completed")
        data = ReportData(
            session_id=session_id, started_at=started, completed_at=completed, timezone=self.settings.timezone,
            status="completed" if status == "completed" else status, profile=profile, config=config, jobs=unique,
            total_discoveries=len(collected), duplicates_merged=merged, source_reports=reports, queries=queries,
            ai_status=ai.status_line(), search_providers=search.provider_names if search else [],
            limitations=build_limitations(self.settings, search, ai, interrupted),
            errors=errors + ai.errors + (search.errors if search else []),
            home_country=str(self.settings.get("candidate.home_country", "Indonesia")),
            new_jobs=new_count, countries_searched=config.all_countries(self.settings.get("search.worldwide_countries", []))
            if config.include_worldwide else list(config.countries),
        )
        if config.include_worldwide:
            data.countries_searched = list(dict.fromkeys(data.countries_searched + ["Remote / worldwide"]))
        try:
            self.repo.save_queries(session_id, queries)
            self.repo.save_source_runs(session_id, reports)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not save run metadata: {exc}")
        result = RunResult(session_id=session_id, status=status, report_dir=None, data=data, errors=errors)
        st.set("Report generation", RUNNING)
        try:
            report_dir, files = self.write_reports(data, collected)
            result.report_dir, result.report_files = report_dir, files
            st.set("Report generation", COMPLETED, str(report_dir))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Report generation failed: {exc}")
            st.set("Report generation", FAILED, str(exc)[:120])
        self.repo.finish_session(session_id, status, data.summary_counts(),
                                 str(result.report_dir) if result.report_dir else None)
        for stage, s in st.stages.items():
            if s == "WAITING":
                st.set(stage, SKIPPED)
        await http.aclose()
        await ai_http.aclose()
        log_event(log, 20, "session.end", session=session_id, status=status, unique=len(unique))
        return result

    async def _recheck_previous(self, http: SafeHttpClient, jobs: list[JobOpportunity], cancelled: asyncio.Event) -> None:
        limit = min(len(jobs), 30)
        sem = asyncio.Semaphore(int(self.settings.get("performance.max_concurrency", 5)))

        async def one(j: JobOpportunity) -> None:
            if cancelled.is_set():
                return
            async with sem:
                page = await fetch_and_extract(http, j.source_url, self.settings, allow_browser=False)
                j.signals["fetch"] = {"ok": page.ok, "status": page.status, "error": page.error,
                                      "error_kind": page.error_kind, "closed_markers": page.closed_markers,
                                      "has_job_posting_ld": bool(page.job_postings),
                                      "valid_through": page.primary_posting.valid_through.isoformat()
                                      if page.primary_posting and page.primary_posting.valid_through else None}
                j.page_fetched = page.ok

        await asyncio.gather(*(one(j) for j in jobs[:limit]))

    # -------------------------------------------------------------- reports
    def write_reports(self, data: ReportData, raws: list[RawDiscovery] | None = None,
                      directory: Path | None = None) -> tuple[Path, dict[str, Path]]:
        base = self.settings.reports_directory
        folder = directory or base / folder_timestamp(data.started_at, data.timezone)
        if directory is None and folder.exists():
            folder = base / f"{folder.name}_{data.session_id[-6:]}"
        folder.mkdir(parents=True, exist_ok=True)
        files: dict[str, Path] = {}
        errors: list[str] = []
        if self.settings.get("reporting.docx_enabled", True):
            try:
                files["docx"] = DocxReportGenerator(data).save(folder / "JOB_SEARCH_REPORT.docx")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if self.settings.get("reporting.xlsx_enabled", True):
            try:
                files["xlsx"] = ExcelReportGenerator(data).save(folder / "JOB_SEARCH_RESULTS.xlsx")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        meta_path = folder / "search_metadata.json"
        meta_path.write_text(json.dumps(data.metadata(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        files["metadata"] = meta_path
        if self.settings.get("reporting.write_raw_discovery_json", True):
            raw_path = folder / "raw_discovery.json"
            payload: dict[str, Any] = {
                "session_id": data.session_id,
                "raw_discoveries": [d.to_dict() for d in raws] if raws is not None
                else self.repo.raw_for_session(data.session_id),
                "opportunities": [j.to_dict() for j in data.sorted_jobs()],
            }
            raw_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            files["raw"] = raw_path
        for kind, path in files.items():
            try:
                self.repo.save_report(data.session_id, kind, str(path))
            except Exception:  # noqa: BLE001
                pass
        if errors:
            data.errors.extend(errors)
            if "docx" not in files and "xlsx" not in files:
                raise RuntimeError("; ".join(errors))
        return folder, files

    def regenerate_reports(self, session_id: str) -> tuple[Path, dict[str, Path]]:
        """Rebuild DOCX/XLSX for a stored session (no network access)."""
        sess = self.repo.get_session(session_id)
        if not sess:
            raise ValueError(f"Unknown session id: {session_id}")
        profile = self.repo.profile_by_id(sess["profile_id"]) if sess.get("profile_id") else None
        jobs = self.repo.session_opportunities(session_id)
        stats = json.loads(sess.get("stats_json") or "{}")
        from src.utils.date_time import from_iso

        started = from_iso(sess["started_at"]) or utc_now()
        data = ReportData(
            session_id=session_id, started_at=started, completed_at=from_iso(sess.get("completed_at")) or started,
            timezone=self.settings.timezone, status=sess["status"], profile=profile or CandidateProfile(),
            config=SearchConfig.from_dict(json.loads(sess["config_json"])), jobs=jobs,
            total_discoveries=int(stats.get("total_discoveries", len(jobs))),
            duplicates_merged=int(stats.get("duplicates_merged", 0)),
            source_reports=self.repo.source_runs(session_id), queries=self.repo.session_queries(session_id),
            ai_status="(regenerated from stored results)", new_jobs=int(stats.get("new_since_previous_searches", 0)),
            limitations=["Report regenerated from stored results; no new searches were performed."],
            countries_searched=SearchConfig.from_dict(json.loads(sess["config_json"])).countries,
        )
        out_dir = self.settings.reports_directory / f"{folder_timestamp(started, data.timezone)}_regenerated_{datetime.now().strftime('%H%M%S')}"
        return self.write_reports(data, None, out_dir)
