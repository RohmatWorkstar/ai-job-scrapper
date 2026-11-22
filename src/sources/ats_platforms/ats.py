"""Official company ATS job boards via their documented public job-board APIs.

* Greenhouse Job Board API   GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs[/{id}]
* Lever Postings API         GET https://api.lever.co/v0/postings/{company}?mode=json
* Ashby Job Posting API      GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
* SmartRecruiters Posting API GET https://api.smartrecruiters.com/v1/companies/{company}/postings[/{id}]

Boards come from ``ats_seed_boards`` in config/search_profiles.yaml plus boards
auto-detected from search results (e.g. a jobs.lever.co/<company> URL). These run
in the late stage so newly detected boards are included. A posting that appears in
a live ATS listing is published on the company's own recruiting system - the
strongest openness evidence available without applying.

Workday has no documented public job API; Workday postings are discovered via
search results and verified through the page itself (robots.txt permitting).
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceRunReport, SourceType
from src.extraction.content_parser import normalise_currency
from src.extraction.html_extractor import html_to_text
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

PLATFORM_SHARE = {"greenhouse": 0.4, "ashby": 0.25, "lever": 0.2, "smartrecruiters": 0.15}
API_MAX_BYTES = 40_000_000  # JSON only; never binary files


class AtsSource(BaseJobSource):
    category = SourceType.ATS
    platform = "ats"
    late_stage = True
    detail_limit_per_board = 15

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:  # pragma: no cover
        return []

    def company_names(self, ctx: DiscoveryContext) -> dict[str, str]:
        """Seed entries may be 'token' or {token: ..., name: ...} (some APIs omit the company name)."""
        out: dict[str, str] = {}
        for entry in (ctx.settings.profiles.get("ats_seed_boards") or {}).get(self.platform, []) or []:
            if isinstance(entry, dict) and entry.get("token") and entry.get("name"):
                out[str(entry["token"]).lower()] = str(entry["name"])
        return out

    def boards(self, ctx: DiscoveryContext) -> tuple[list[str], int]:
        raw = (ctx.settings.profiles.get("ats_seed_boards") or {}).get(self.platform, []) or []
        seeds = [str(e["token"]) if isinstance(e, dict) else str(e) for e in raw if e]
        discovered = sorted(ctx.ats_tokens.get(self.platform, set()))
        limit = max(2, int(int(ctx.settings.get("budget.max_ats_boards", 40)) * PLATFORM_SHARE.get(self.platform, 0.2)))
        ordered: list[str] = []
        # Boards found through search for this candidate first: they are known to have relevant roles.
        for b in discovered + seeds:
            if b.lower() not in {x.lower() for x in ordered}:
                ordered.append(b)
        return ordered[:limit], len(discovered)

    async def fetch_board(self, token: str, ctx: DiscoveryContext) -> list[RawDiscovery]:
        raise NotImplementedError

    async def health_check(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        boards, _ = self.boards(ctx)
        if not boards:
            return False, "no boards configured"
        try:
            await self.fetch_board(boards[0], ctx)
            return True, f"board '{boards[0]}' reachable"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _get_json(self, url: str, ctx: DiscoveryContext, params: dict[str, Any] | None = None) -> Any:
        # Large company boards (e.g. hundreds of postings with descriptions) can exceed the HTML cap.
        res = await ctx.http.get(url, params=params, respect_robots=False, headers={"Accept": "application/json"},
                                 max_bytes=API_MAX_BYTES)
        if not res.ok:
            raise http_error_to_exception(f"{self.name} {url.split('/')[-1] or url}", res)
        return res.json()

    async def run(self, ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        boards, n_discovered = self.boards(ctx)
        if not boards:
            report.notes.append("No boards configured or discovered.")
            return []
        report.notes.append(f"{len(boards)} board(s) crawled ({n_discovered} auto-discovered from search results).")
        names = self.company_names(ctx)
        results: list[RawDiscovery] = []
        dropped_title = dropped_date = 0

        async def one(token: str) -> None:
            nonlocal dropped_title, dropped_date
            if ctx.cancelled.is_set():
                return
            try:
                items = await self.fetch_board(token, ctx)
            except Exception as exc:  # noqa: BLE001 - isolate per board
                ctx.record_query(self.name, self.category, f"board:{token}", "error", 0, str(exc))
                report.errors.append(f"board '{token}': {exc}")
                return
            report.queries_executed += 1
            report.items_scanned += len(items)
            if token.lower() in names:
                for d in items:
                    d.company = names[token.lower()]
            kept: list[RawDiscovery] = []
            for d in items:
                dec = ctx.prefilter.decide(d.title, d.tags, d.text, d.published_at)
                if dec.keep:
                    kept.append(d)
                elif dec.reason == "outside publication window":
                    dropped_date += 1
                else:
                    dropped_title += 1
            kept = await self.enrich(token, kept[: self.detail_limit_per_board * 3], ctx)
            results.extend(kept)
            ctx.record_query(self.name, self.category, f"board:{token} ({len(items)} postings)",
                             "ok" if kept else "no_results", len(kept))

        await asyncio.gather(*(one(t) for t in boards))
        report.notes.append(f"Scanned {report.items_scanned} postings: kept {len(results)}, {dropped_title} unrelated "
                            f"titles, {dropped_date} outside the publication window.")
        return results

    async def enrich(self, token: str, items: list[RawDiscovery], ctx: DiscoveryContext) -> list[RawDiscovery]:
        return items

    def _base(self, *, token: str, url: str, title: str, company: str | None, published: Any, ext_id: Any,
              text: str = "", apply_url: str | None = None, location: str | None = None) -> RawDiscovery:
        return RawDiscovery(
            source_type=self.category, source_name=f"{self.name} ({token})", source_url=url, title=(title or "").strip(),
            retrieved_at=utc_now(), text=text, company=company or token.replace("-", " ").title(),
            application_url=apply_url or url, location=location, published_at=parse_datetime(published),
            published_at_evidence=f"{self.name} API publication field" if published else "",
            external_id=f"{self.platform}:{token.lower()}:{ext_id}", official_channel=True, listed_in_live_feed=True,
            structured=True, content_kind=ContentKind.JOB_POSTING,
        )


class GreenhouseSource(AtsSource):
    name = "Greenhouse"
    platform = "greenhouse"
    description = "Greenhouse Job Board API (company boards)"

    async def fetch_board(self, token: str, ctx: DiscoveryContext) -> list[RawDiscovery]:
        data = await self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", ctx)
        out = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name")
            d = self._base(token=token, url=j.get("absolute_url") or "", title=j.get("title", ""),
                           company=j.get("company_name"), published=j.get("first_published") or None,
                           ext_id=j.get("id"), location=loc)
            if not j.get("first_published") and j.get("updated_at"):
                d.raw["updated_at"] = j.get("updated_at")
            d.raw["gh_id"] = j.get("id")
            out.append(d)
        return [d for d in out if d.source_url]

    async def enrich(self, token: str, items: list[RawDiscovery], ctx: DiscoveryContext) -> list[RawDiscovery]:
        async def detail(d: RawDiscovery) -> None:
            try:
                j = await self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{d.raw['gh_id']}", ctx)
                d.text = html_to_text(j.get("content"), 6000)
            except Exception as exc:  # noqa: BLE001 - description is optional
                d.raw["detail_error"] = str(exc)

        await asyncio.gather(*(detail(d) for d in items[: self.detail_limit_per_board]))
        return items


class LeverSource(AtsSource):
    name = "Lever"
    platform = "lever"
    description = "Lever Postings API (company boards)"

    async def fetch_board(self, token: str, ctx: DiscoveryContext) -> list[RawDiscovery]:
        data = await self._get_json(f"https://api.lever.co/v0/postings/{token}", ctx, {"mode": "json"})
        out = []
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            d = self._base(token=token, url=j.get("hostedUrl") or "", title=j.get("text", ""), company=None,
                           published=j.get("createdAt"), ext_id=j.get("id"),
                           text=(j.get("descriptionPlain") or "") + "\n" + "\n".join(
                               f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []),
                           apply_url=j.get("applyUrl"), location=cats.get("location"))
            d.published_at_evidence = "Lever createdAt (posting creation time)"
            d.employment_type = cats.get("commitment")
            d.work_arrangement = j.get("workplaceType") if j.get("workplaceType") not in (None, "unspecified") else None
            d.country = j.get("country")
            sr = j.get("salaryRange") or {}
            if sr.get("min"):
                d.salary_min, d.salary_max = float(sr["min"]), (float(sr["max"]) if sr.get("max") else None)
                d.salary_currency = normalise_currency(sr.get("currency"))
                d.salary_period = {"per-year-salary": "year", "per-month-salary": "month",
                                   "per-hour-wage": "hour"}.get(str(sr.get("interval")), sr.get("interval"))
            d.tags = [cats.get("team") or "", cats.get("department") or ""]
            out.append(d)
        return [d for d in out if d.source_url]


class AshbySource(AtsSource):
    name = "Ashby"
    platform = "ashby"
    description = "Ashby Job Posting API (company boards)"

    async def fetch_board(self, token: str, ctx: DiscoveryContext) -> list[RawDiscovery]:
        data = await self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}", ctx,
                                    {"includeCompensation": "true"})
        out = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            addr = ((j.get("address") or {}).get("postalAddress") or {})
            d = self._base(token=token, url=j.get("jobUrl") or "", title=j.get("title", ""), company=None,
                           published=j.get("publishedAt"), ext_id=j.get("id"), text=j.get("descriptionPlain") or "",
                           apply_url=j.get("applyUrl"), location=j.get("location"))
            d.country = addr.get("addressCountry")
            wt = str(j.get("workplaceType") or "").lower()
            d.work_arrangement = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(
                wt, "remote" if j.get("isRemote") else None)
            d.employment_type = j.get("employmentType")
            comp = j.get("compensation") or {}
            summary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
            if summary and j.get("shouldDisplayCompensationOnJobPostings", True):
                d.salary_text = str(summary)
            d.tags = [j.get("department") or "", j.get("team") or ""]
            out.append(d)
        return [d for d in out if d.source_url]


class SmartRecruitersSource(AtsSource):
    name = "SmartRecruiters"
    platform = "smartrecruiters"
    description = "SmartRecruiters Posting API (company boards)"
    detail_limit_per_board = 10

    async def fetch_board(self, token: str, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out: dict[str, RawDiscovery] = {}
        terms = [k for k in ctx.plan.keywords[:3]] or [None]
        for term in terms:
            params: dict[str, Any] = {"limit": 100}
            if term:
                params["q"] = term
            data = await self._get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings", ctx, params)
            for j in data.get("content", []):
                loc = j.get("location") or {}
                pid = str(j.get("id"))
                if pid in out:
                    continue
                d = self._base(token=token, url=f"https://jobs.smartrecruiters.com/{token}/{pid}", title=j.get("name", ""),
                               company=(j.get("company") or {}).get("name"), published=j.get("releasedDate"),
                               ext_id=pid, location=loc.get("fullLocation") or loc.get("city"))
                d.country = loc.get("country")
                d.work_arrangement = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
                d.employment_type = (j.get("typeOfEmployment") or {}).get("label")
                d.seniority = (j.get("experienceLevel") or {}).get("label")
                d.tags = [(j.get("function") or {}).get("label") or ""]
                d.raw["ref"] = j.get("ref")
                out[pid] = d
        return list(out.values())

    async def enrich(self, token: str, items: list[RawDiscovery], ctx: DiscoveryContext) -> list[RawDiscovery]:
        async def detail(d: RawDiscovery) -> None:
            ref = d.raw.get("ref")
            if not ref or not str(ref).startswith("https://api.smartrecruiters.com/"):
                return
            try:
                j = await self._get_json(str(ref), ctx)
                sections = ((j.get("jobAd") or {}).get("sections")) or {}
                parts = [html_to_text((sections.get(k) or {}).get("text")) for k in
                         ("jobDescription", "qualifications", "additionalInformation")]
                d.text = "\n\n".join(p for p in parts if p)[:6000]
                if j.get("applyUrl"):
                    d.application_url = j["applyUrl"]
            except Exception as exc:  # noqa: BLE001
                d.raw["detail_error"] = str(exc)

        await asyncio.gather(*(detail(d) for d in items[: self.detail_limit_per_board]))
        return items
