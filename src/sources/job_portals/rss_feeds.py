"""Public RSS job feeds requiring no API key or authentication.

* We Work Remotely  GET https://weworkremotely.com/categories/<category>.rss
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceRunReport, SourceType
from src.extraction.html_extractor import html_to_text
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

_WWR_BASE = "https://weworkremotely.com/categories"
_WWR_FEEDS = {
    "programming": f"{_WWR_BASE}/remote-programming-jobs.rss",
    "fullstack": f"{_WWR_BASE}/remote-full-stack-programming-jobs.rss",
    "backend": f"{_WWR_BASE}/remote-back-end-programming-jobs.rss",
    "frontend": f"{_WWR_BASE}/remote-front-end-programming-jobs.rss",
    "devops": f"{_WWR_BASE}/remote-devops-sysadmin-jobs.rss",
}


def _elem_text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    # Handle namespaced elements by matching local name.
    for child in parent:
        local = child.tag.rsplit("}", 1)[-1]
        if local == tag and child.text:
            return child.text.strip()
    return ""


def _split_wwr_title(raw_title: str) -> tuple[str | None, str]:
    """WWR RSS titles follow 'Company Name: Job Title'."""
    t = (raw_title or "").strip()
    if ":" in t:
        company, role = t.split(":", 1)
        company, role = company.strip(), role.strip()
        if company and role and len(company) <= 80:
            return company, role
    return None, t


class WeWorkRemotelySource(BaseJobSource):
    name = "We Work Remotely"
    category = SourceType.JOB_PORTAL
    description = "Remote engineering RSS feeds (weworkremotely.com)"

    def applicable(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if ctx.config.work_arrangements and "remote" not in ctx.config.work_arrangements:
            return False, "Skipped: remote-only board and remote work was not selected."
        return True, ""

    def feed_urls(self, ctx: DiscoveryContext) -> list[str]:
        fams = set(ctx.profile.role_families or [])
        chosen: list[str] = []
        if fams & {"fullstack"}:
            chosen.append(_WWR_FEEDS["fullstack"])
        if fams & {"backend", "node", "python", "php", "java"}:
            chosen.append(_WWR_FEEDS["backend"])
        if fams & {"frontend"}:
            chosen.append(_WWR_FEEDS["frontend"])
        if fams & {"devops"}:
            chosen.append(_WWR_FEEDS["devops"])
        if _WWR_FEEDS["programming"] not in chosen:
            chosen.append(_WWR_FEEDS["programming"])
        return list(dict.fromkeys(chosen))[:2]

    def parse_rss(self, xml_text: str) -> list[RawDiscovery]:
        root = ET.fromstring(xml_text)
        now = utc_now()
        out: list[RawDiscovery] = []
        for item in root.findall(".//item"):
            raw_title = _elem_text(item, "title")
            link = _elem_text(item, "link") or _elem_text(item, "guid")
            if not link or not raw_title:
                continue
            company, title = _split_wwr_title(raw_title)
            region = _elem_text(item, "region")
            category = _elem_text(item, "category")
            job_type = _elem_text(item, "type")
            pub_date = _elem_text(item, "pubDate")
            guid = _elem_text(item, "guid") or link
            desc_html = _elem_text(item, "description")
            text = html_to_text(desc_html, 40000)
            tags = [t for t in (category, job_type) if t]
            d = RawDiscovery(
                source_type=self.category,
                source_name="We Work Remotely",
                source_url=link,
                title=title,
                retrieved_at=now,
                text=text,
                snippet=text[:300],
                company=company,
                location=region or None,
                application_url=link,
                published_at=parse_datetime(pub_date),
                published_at_evidence="We Work Remotely RSS pubDate" if pub_date else "",
                work_arrangement="remote",
                employment_type=job_type or None,
                location_restrictions=[region] if region else [],
                tags=tags,
                external_id=f"wwr:{guid}",
                listed_in_live_feed=True,
                structured=True,
                content_kind=ContentKind.JOB_POSTING,
            )
            out.append(d)
        return out

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:  # pragma: no cover
        return []

    async def health_check(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        url = self.feed_urls(ctx)[0]
        res = await ctx.http.get(
            url,
            respect_robots=False,
            use_cache=True,
            headers={"Accept": "application/rss+xml, application/xml, text/xml"},
        )
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
        for url in self.feed_urls(ctx):
            if ctx.cancelled.is_set() or report.items_scanned >= limit:
                break
            res = await ctx.http.get(
                url,
                respect_robots=False,
                use_cache=True,
                headers={"Accept": "application/rss+xml, application/xml, text/xml"},
                max_bytes=10_000_000,
            )
            if not res.ok:
                exc = http_error_to_exception(self.name, res)
                ctx.record_query(self.name, self.category, url, "error", 0, str(exc))
                report.errors.append(str(exc))
                if exc.permanent:
                    break
                continue
            try:
                items = self.parse_rss(res.text)
            except ET.ParseError as exc:
                ctx.record_query(self.name, self.category, url, "error", 0, f"Malformed RSS XML: {exc}")
                report.errors.append(f"{self.name}: malformed RSS XML")
                continue
            report.queries_executed += 1
            before = len(kept)
            for d in items:
                if report.items_scanned >= limit:
                    break
                if d.source_url in seen:
                    continue
                seen.add(d.source_url)
                report.items_scanned += 1
                dec = ctx.prefilter.decide(d.title, d.tags, d.text, d.published_at)
                if dec.keep:
                    kept.append(d)
                elif dec.reason == "outside publication window":
                    dropped_date += 1
                else:
                    dropped_title += 1
            ctx.record_query(
                self.name,
                self.category,
                url + (" [cached]" if res.from_cache else ""),
                "ok" if len(kept) > before else "no_results",
                len(kept) - before,
            )
        report.notes.append(
            f"Scanned {report.items_scanned} listings: kept {len(kept)}, "
            f"{dropped_title} unrelated job titles, {dropped_date} outside the publication window."
        )
        return kept
