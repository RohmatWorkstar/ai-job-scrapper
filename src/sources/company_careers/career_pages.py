"""Official company career pages.

Two discovery paths:
1. Pages listed in ``company_career_pages`` (config/search_profiles.yaml) are fetched
   directly (robots.txt enforced). schema.org ``JobPosting`` JSON-LD is extracted; if
   absent, links whose text matches the candidate's roles are collected as leads.
2. Search-provider queries for "<role> careers apply" excluding the big portals,
   which surface company-owned career sites.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceRunReport, SourceType
from src.extraction.html_extractor import extract_from_html
from src.sources.base import DiscoveryContext
from src.sources.search_engine.web_search import SearchBackedSource
from src.utils.date_time import utc_now
from src.utils.validators import domain_of


class CompanyCareerPagesSource(SearchBackedSource):
    name = "Company Career Pages"
    category = SourceType.COMPANY_CAREERS
    description = "Configured career pages (JSON-LD JobPosting) + careers-page web search"

    def pages(self, ctx: DiscoveryContext) -> list[dict[str, str]]:
        return [p for p in (ctx.settings.profiles.get("company_career_pages") or []) if isinstance(p, dict) and p.get("url")]

    def is_configured(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if self.pages(ctx) or (ctx.search is not None and ctx.search.available):
            return True, ""
        return False, "No career pages configured and no search API configured"

    def source_label(self, query: QuerySpec) -> str:
        return "Company careers (web search)"

    async def run(self, ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        results: list[RawDiscovery] = []
        if ctx.search is not None and ctx.search.available:
            results.extend(await super().run(ctx, report))
        for page in self.pages(ctx):
            if ctx.cancelled.is_set():
                break
            results.extend(await self.crawl_page(page, ctx, report))
        return results

    async def crawl_page(self, page: dict[str, str], ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        url, company = page["url"], page.get("company")
        res = await ctx.http.get(url, respect_robots=True, headers={"Accept": "text/html,application/xhtml+xml"})
        report.queries_executed += 1
        if not res.ok:
            msg = f"{company or domain_of(url)}: {res.error}"
            ctx.record_query(self.name, self.category, url, "error", 0, msg)
            report.errors.append(msg)
            return []
        closed = ctx.settings.get("verification.closed_indicators", []) or []
        extraction = extract_from_html(res.text, res.final_url or url, closed)
        out: list[RawDiscovery] = []
        now = utc_now()
        if extraction.job_postings:
            for jp in extraction.job_postings:
                sub = type(extraction)(url=url, final_url=extraction.final_url, ok=True, job_postings=[jp],
                                       text=jp.description, title=jp.title, retrieved_at=now)
                d = sub.to_discovery(self.category, company or domain_of(url), jp.url or url)
                d.company = d.company or company
                d.official_channel = True
                out.append(d)
        else:
            soup = BeautifulSoup(res.text, "html.parser")
            for a in soup.find_all("a", href=True):
                text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                if not (4 <= len(text) <= 120):
                    continue
                keep, _ = ctx.prefilter.title_relevant(text)
                if not keep:
                    continue
                link = urljoin(res.final_url or url, a["href"])
                if not link.startswith("http") or domain_of(link) != domain_of(url) and "greenhouse" not in link \
                        and "lever.co" not in link and "ashbyhq" not in link:
                    continue
                out.append(RawDiscovery(source_type=self.category, source_name=company or domain_of(url), source_url=link,
                                        title=text, retrieved_at=now, company=company, official_channel=True,
                                        content_kind=ContentKind.SEARCH_RESULT, query=url))
        report.items_scanned += len(out)
        ctx.record_query(self.name, self.category, url, "ok" if out else "no_results", len(out))
        return out
