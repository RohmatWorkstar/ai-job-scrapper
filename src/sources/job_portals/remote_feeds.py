"""Public job-board APIs that explicitly offer free programmatic access.

All schemas below were verified against live responses (2026-11-22). Each
provider's attribution terms are honoured: the original listing URL and the
provider name are kept on every result and shown in reports. Responses are
cached on disk (see performance.cache_ttl_hours) because several providers ask
for only a few requests per day.

* Remotive      GET https://remotive.com/api/remote-jobs?category=...   (max ~4 calls/day requested)
* Remote OK     GET https://remoteok.com/api                           (first element = legal notice)
* Arbeitnow     GET https://www.arbeitnow.com/api/job-board-api?page=N
* Jobicy        GET https://jobicy.com/api/v2/remote-jobs?count=&tag=
* Himalayas     GET https://himalayas.app/jobs/api?limit=&offset=
"""

from __future__ import annotations

from typing import Any

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceRunReport, SourceType
from src.extraction.content_parser import normalise_currency, parse_salary
from src.extraction.html_extractor import html_to_text
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

EUROPE = {"Germany", "Netherlands", "United Kingdom", "Ireland", "France", "Spain", "Portugal", "Poland", "Sweden",
          "Switzerland"}


class FeedSource(BaseJobSource):
    category = SourceType.JOB_PORTAL
    provider_name = "feed"
    retries: int | None = None  # None = client default

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        raise NotImplementedError

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        raise NotImplementedError

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        return True, ""

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:  # pragma: no cover
        return []

    async def health_check(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        url, params = self.requests(ctx)[0]
        res = await ctx.http.get(url, params=params, respect_robots=False, use_cache=True,
                                 headers={"Accept": "application/json"})
        return res.ok, "reachable" if res.ok else (res.error or "error")

    async def run(self, ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        ok, why = self.applicable(ctx)
        if not ok:
            report.notes.append(why)
            return []
        limit = int(ctx.settings.get("budget.max_feed_items_per_source", 400))
        seen: set[str] = set()
        kept: list[RawDiscovery] = []
        dropped_date = dropped_title = 0
        for url, params in self.requests(ctx):
            if ctx.cancelled.is_set() or report.items_scanned >= limit:
                break
            res = await ctx.http.get(url, params=params, respect_robots=False, use_cache=True,
                                     headers={"Accept": "application/json"}, max_bytes=20_000_000,
                                     retries=self.retries)
            label = url +(("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else "")
            if not res.ok:
                exc = http_error_to_exception(self.name, res)
                ctx.record_query(self.name, self.category, label, "error", 0, str(exc))
                report.errors.append(str(exc))
                if exc.permanent:
                    break
                continue
            try:
                items = self.parse(res.json(), ctx)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                ctx.record_query(self.name, self.category, label, "error", 0, f"Unexpected response format: {exc}")
                report.errors.append(f"{self.name}: unexpected response format ({type(exc).__name__})")
                continue
            report.queries_executed += 1
            count_before = len(kept)
            for d in items:
                if report.items_scanned >= limit:
                    break
                if d.source_url in seen:
                    continue
                seen.add(d.source_url)
                report.items_scanned += 1
                decision = ctx.prefilter.decide(d.title, d.tags, d.text, d.published_at)
                if decision.keep:
                    kept.append(d)
                elif decision.reason == "outside publication window":
                    dropped_date += 1
                else:
                    dropped_title += 1
            ctx.record_query(self.name, self.category, label + (" [cached]" if res.from_cache else ""),
                             "ok" if len(kept) > count_before else "no_results", len(kept) - count_before)
        report.notes.append(
            f"Scanned {report.items_scanned} listings: kept {len(kept)}, "
            f"{dropped_title} unrelated job titles, {dropped_date} outside the publication window."
        )
        return kept

    def _base(self, *, url: str, title: str, company: str | None, text: str, published: Any,
              apply_url: str | None = None, location: str | None = None, ext_id: Any = None,
              tags: list[str] | None = None) -> RawDiscovery:
        return RawDiscovery(
            source_type=self.category, source_name=self.provider_name, source_url=url, title=title.strip(),
            retrieved_at=utc_now(), text=text, company=(company or None), location=location,
            application_url=apply_url, published_at=parse_datetime(published),
            published_at_evidence=f"{self.provider_name} API publication field" if published else "",
            tags=[str(t) for t in (tags or [])], external_id=f"{self.provider_name.lower()}:{ext_id}" if ext_id else None,
            listed_in_live_feed=True, structured=True, content_kind=ContentKind.JOB_POSTING,
        )


class RemotiveSource(FeedSource):
    name = "Remotive"
    provider_name = "Remotive"
    description = "Remote jobs API (remotive.com) - attribution required"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if ctx.config.work_arrangements and "remote" not in ctx.config.work_arrangements:
            return False, "Skipped: remote-only board and remote work was not selected."
        return True, ""

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        cats = ["software-development"]
        fams = set(ctx.profile.role_families)
        if fams & {"devops"}:
            cats.append("devops")
        if fams & {"data_engineering", "data_science"}:
            cats.append("data")
        if fams & {"ai_ml"}:
            cats.append("artificial-intelligence")
        if fams & {"qa"}:
            cats.append("qa")
        return [("https://remotive.com/api/remote-jobs", {"category": c}) for c in cats[:2]]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out = []
        for j in data.get("jobs", []):
            d = self._base(url=j["url"], title=j.get("title", ""), company=j.get("company_name"),
                           text=html_to_text(j.get("description"), 40000), published=j.get("publication_date"),
                           apply_url=j.get("url"), location=j.get("candidate_required_location"), ext_id=j.get("id"),
                           tags=j.get("tags"))
            d.work_arrangement = "remote"
            d.employment_type = j.get("job_type")
            d.location_restrictions = [j["candidate_required_location"]] if j.get("candidate_required_location") else []
            sal = parse_salary(j.get("salary") or "")
            if sal:
                d.salary_min, d.salary_max, d.salary_currency, d.salary_period, d.salary_text = (
                    sal.minimum, sal.maximum, sal.currency, sal.period, j.get("salary"))
            elif j.get("salary"):
                d.salary_text = str(j["salary"])
            out.append(d)
        return out


class RemoteOKSource(FeedSource):
    name = "Remote OK"
    provider_name = "Remote OK"
    description = "Remote jobs API (remoteok.com) - attribution and link-back required"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        return RemotiveSource.applicable(self, ctx)  # type: ignore[arg-type]

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        return [("https://remoteok.com/api", {})]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out = []
        for j in data[1:] if isinstance(data, list) else []:
            if not isinstance(j, dict) or not j.get("url"):
                continue
            tags = j.get("tags") if isinstance(j.get("tags"), list) else []
            d = self._base(url=j["url"], title=j.get("position", ""), company=j.get("company"),
                           text=html_to_text(j.get("description"), 40000), published=j.get("date") or j.get("epoch"),
                           apply_url=j.get("apply_url") or j.get("url"), location=j.get("location"), ext_id=j.get("id"),
                           tags=tags)
            d.work_arrangement = "remote"
            lo, hi = j.get("salary_min"), j.get("salary_max")
            try:
                lo, hi = float(lo or 0), float(hi or 0)
            except (TypeError, ValueError):
                lo = hi = 0
            if lo > 0:
                d.salary_min, d.salary_max, d.salary_currency, d.salary_period = lo, (hi or None), "USD", "year"
            if j.get("location"):
                d.location_restrictions = [str(j["location"])]
            out.append(d)
        return out


class ArbeitnowSource(FeedSource):
    name = "Arbeitnow"
    provider_name = "Arbeitnow"
    description = "Europe-focused job board API (arbeitnow.com)"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if ctx.config.include_worldwide or EUROPE & set(ctx.config.countries):
            return True, ""
        return False, "Skipped: Europe-focused board and no European/worldwide geography selected."

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        return [("https://www.arbeitnow.com/api/job-board-api", {"page": p}) for p in (1, 2)]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out = []
        for j in data.get("data", []):
            d = self._base(url=j["url"], title=j.get("title", ""), company=j.get("company_name"),
                           text=html_to_text(j.get("description"), 40000), published=j.get("created_at"),
                           apply_url=j.get("url"), location=j.get("location"), ext_id=j.get("slug"), tags=j.get("tags"))
            d.work_arrangement = "remote" if j.get("remote") else None
            d.employment_type = (j.get("job_types") or [None])[0]
            out.append(d)
        return out


class JobicySource(FeedSource):
    name = "Jobicy"
    provider_name = "Jobicy"
    description = "Remote jobs API (jobicy.com) - attribution required"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        return RemotiveSource.applicable(self, ctx)  # type: ignore[arg-type]

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        tags = [k for k in ctx.plan.keywords if " " not in k][:3] or ["developer"]
        return [("https://jobicy.com/api/v2/remote-jobs", {"count": 50, "tag": t}) for t in tags]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out = []
        for j in data.get("jobs", []):
            d = self._base(url=j["url"], title=j.get("jobTitle", ""), company=j.get("companyName"),
                           text=html_to_text(j.get("jobDescription") or j.get("jobExcerpt"), 40000),
                           published=j.get("pubDate"), apply_url=j.get("url"), location=j.get("jobGeo"),
                           ext_id=j.get("id"), tags=(j.get("jobIndustry") or []))
            d.work_arrangement = "remote"
            d.employment_type = (j.get("jobType") or [None])[0] if isinstance(j.get("jobType"), list) else j.get("jobType")
            d.seniority = j.get("jobLevel")
            if j.get("jobGeo"):
                d.location_restrictions = [str(j["jobGeo"])]
            if j.get("annualSalaryMin"):
                try:
                    d.salary_min = float(j["annualSalaryMin"])
                    d.salary_max = float(j["annualSalaryMax"]) if j.get("annualSalaryMax") else None
                    d.salary_currency = normalise_currency(j.get("salaryCurrency"))
                    d.salary_period = "year"
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out


class HimalayasSource(FeedSource):
    name = "Himalayas"
    provider_name = "Himalayas"
    description = "Remote jobs API (himalayas.app), newest listings"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        return RemotiveSource.applicable(self, ctx)  # type: ignore[arg-type]

    retries = 0  # the API is slow on uncached pages; fail fast instead of waiting through retries

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        # The API returns at most 20 jobs per page regardless of `limit`.
        return [("https://himalayas.app/jobs/api", {"limit": 20, "offset": off}) for off in (0, 20, 40, 60)]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out = []
        for j in data.get("jobs", []):
            url = j.get("guid") or j.get("applicationLink")
            if not url:
                continue
            restrictions = [str(x) for x in (j.get("locationRestrictions") or [])]
            d = self._base(url=url, title=j.get("title", ""), company=j.get("companyName"),
                           text=html_to_text(j.get("description") or j.get("excerpt"), 40000),
                           published=j.get("pubDate"), apply_url=j.get("applicationLink"),
                           location=", ".join(restrictions) or None, ext_id=url,
                           tags=[str(c) for c in (j.get("categories") or [])])
            d.work_arrangement = "remote"
            d.employment_type = j.get("employmentType")
            sen = j.get("seniority")
            d.seniority = sen[0] if isinstance(sen, list) and sen else sen
            d.location_restrictions = restrictions
            if j.get("minSalary"):
                try:
                    d.salary_min = float(j["minSalary"])
                    d.salary_max = float(j["maxSalary"]) if j.get("maxSalary") else None
                    d.salary_currency = normalise_currency(j.get("currency"))
                    d.salary_period = {"hourly": "hour", "monthly": "month", "yearly": "year", "annual": "year"}.get(
                        str(j.get("salaryPeriod") or "").lower(), j.get("salaryPeriod"))
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out


class WorkingNomadsSource(FeedSource):
    name = "Working Nomads"
    provider_name = "Working Nomads"
    description = "Remote jobs API (workingnomads.com) - public JSON feed"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        return RemotiveSource.applicable(self, ctx)  # type: ignore[arg-type]

    def requests(self, ctx: DiscoveryContext) -> list[tuple[str, dict[str, Any]]]:
        return [("https://www.workingnomads.com/api/exposed_jobs/", {})]

    def parse(self, data: Any, ctx: DiscoveryContext) -> list[RawDiscovery]:
        out: list[RawDiscovery] = []
        for j in data if isinstance(data, list) else []:
            if not isinstance(j, dict) or not j.get("url"):
                continue
            raw_tags = j.get("tags") or ""
            tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()]
            cat = str(j.get("category_name") or "").strip()
            if cat and cat not in tags:
                tags.append(cat)
            loc = str(j.get("location") or "").strip() or None
            d = self._base(
                url=str(j["url"]),
                title=str(j.get("title") or ""),
                company=str(j.get("company_name") or "").strip() or None,
                text=html_to_text(j.get("description"), 40000),
                published=j.get("pub_date"),
                apply_url=str(j["url"]),
                location=loc,
                ext_id=str(j["url"]),
                tags=tags,
            )
            d.work_arrangement = "remote"
            if loc:
                d.location_restrictions = [loc]
            out.append(d)
        return out
