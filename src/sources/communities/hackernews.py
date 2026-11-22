"""Hacker News "Ask HN: Who is hiring?" threads via the public HN Search API (Algolia).

GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who is hiring
GET https://hn.algolia.com/api/v1/search?tags=comment,story_<id>&query=<keyword>
Only top-level comments (job posts by employers) are used. No credentials required.
"""

from __future__ import annotations

import re

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceRunReport, SourceType
from src.extraction.html_extractor import html_to_text
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

API = "https://hn.algolia.com/api/v1"
_ROLE_SEG = re.compile(r"(?i)(engineer|developer|programmer|architect|devops|sre|scientist|analyst|full[\s-]?stack|"
                       r"back[\s-]?end|front[\s-]?end|mobile|data|ml|ai)")


def parse_header(text: str) -> tuple[str | None, str | None, str | None]:
    """HN convention: 'Company | Role | Location | REMOTE | ...' on the first line."""
    first = text.strip().split("\n", 1)[0]
    segs = [s.strip() for s in re.split(r"\s+\|\s+|\s+\|\||\|", first) if s.strip()]
    if len(segs) < 2:
        return None, None, None
    company = segs[0][:80]
    role = next((s for s in segs[1:] if _ROLE_SEG.search(s) and len(s) < 120), None)
    location = next((s for s in segs[1:] if s is not role and re.search(
        r"(?i)remote|onsite|on-site|hybrid|[A-Z][a-z]+,\s*[A-Z]{2}|london|berlin|singapore|new york|san francisco|"
        r"europe|usa|us\b|anywhere|worldwide|asia", s)), None)
    return company, role, location


class HackerNewsHiringSource(BaseJobSource):
    name = "Hacker News (Who is hiring)"
    category = SourceType.COMMUNITY
    description = "Monthly 'Ask HN: Who is hiring?' employer posts via HN Search API"

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:  # pragma: no cover
        return []

    async def latest_story(self, ctx: DiscoveryContext) -> tuple[str, str] | None:
        res = await ctx.http.get(f"{API}/search_by_date",
                                 params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5},
                                 respect_robots=False, use_cache=True, cache_ttl_hours=6)
        if not res.ok:
            raise http_error_to_exception(self.name, res)
        for hit in res.json().get("hits", []):
            title = hit.get("title") or ""
            if title.lower().startswith("ask hn: who is hiring"):
                return str(hit["objectID"]), title
        return None

    async def run(self, ctx: DiscoveryContext, report: SourceRunReport) -> list[RawDiscovery]:
        try:
            story = await self.latest_story(ctx)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(str(exc))
            ctx.record_query(self.name, self.category, "latest who-is-hiring thread", "error", 0, str(exc))
            return []
        if not story:
            report.notes.append("No 'Who is hiring?' thread found.")
            return []
        story_id, title = story
        report.notes.append(f"Thread: {title}")
        seen: set[str] = set()
        out: list[RawDiscovery] = []
        now = utc_now()
        for kw in ctx.plan.keywords[:5]:
            if ctx.cancelled.is_set():
                break
            res = await ctx.http.get(f"{API}/search", params={"tags": f"comment,story_{story_id}", "query": kw,
                                                              "hitsPerPage": 60},
                                     respect_robots=False, use_cache=True, cache_ttl_hours=2)
            report.queries_executed += 1
            if not res.ok:
                exc = http_error_to_exception(self.name, res)
                report.errors.append(str(exc))
                ctx.record_query(self.name, self.category, f"{title} :: {kw}", "error", 0, str(exc))
                continue
            found = 0
            for hit in res.json().get("hits", []):
                if str(hit.get("parent_id")) != story_id or hit["objectID"] in seen:
                    continue
                seen.add(hit["objectID"])
                report.items_scanned += 1
                text = html_to_text(hit.get("comment_text"), 6000)
                company, role, location = parse_header(text)
                created = parse_datetime(hit.get("created_at"))
                d = RawDiscovery(
                    source_type=self.category, source_name="Hacker News", retrieved_at=now,
                    source_url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    title=role or (text.split("\n")[0][:120]), text=text, snippet=text[:300], company=company,
                    location=location, published_at=created, post_created_at=created,
                    published_at_evidence="HN comment created_at (post date)", content_kind=ContentKind.COMMUNITY_POST,
                    query=kw, external_id=f"hn:{hit['objectID']}",
                )
                if not ctx.prefilter.date_ok(created):
                    continue
                keep, _ = ctx.prefilter.title_relevant(d.title, [], text)
                if keep:
                    out.append(d)
                    found += 1
            ctx.record_query(self.name, self.category, f"{title} :: {kw}", "ok" if found else "no_results", found)
        return out
