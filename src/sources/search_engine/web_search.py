"""General web search discovery + shared helpers for all search-provider-backed sources."""

from __future__ import annotations

import re

from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceType
from src.sources.base import BaseJobSource, DiscoveryContext
from src.sources.search_engine.providers import SearchHit
from src.utils.date_time import utc_now
from src.utils.validators import domain_of

ATS_URL_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(r"(?i)^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"(?i)^https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"(?i)^https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "smartrecruiters": re.compile(r"(?i)^https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
}
_ATS_RESERVED = {"embed", "jobs", "job", "search", "api", "v1", "apply", "careers", "oneclick-ui", "sitemap"}

SOCIAL_DOMAINS = ("linkedin.com/posts", "linkedin.com/feed", "x.com", "twitter.com", "facebook.com", "instagram.com",
                  "threads.net")
COMMUNITY_DOMAINS = ("reddit.com", "news.ycombinator.com", "dev.to", "kaskus.co.id")


def detect_ats(url: str) -> tuple[str, str] | None:
    for platform, pat in ATS_URL_PATTERNS.items():
        m = pat.match(url)
        if m:
            token = m.group(1)
            if token.lower() not in _ATS_RESERVED:
                return platform, token
    return None


def classify_url(url: str, default: SourceType) -> tuple[SourceType, ContentKind, bool]:
    """Infer (source_type, content_kind, official_channel) from where a URL lives."""
    low = url.lower()
    if detect_ats(url) or "myworkdayjobs.com" in low:
        return SourceType.ATS, ContentKind.JOB_POSTING, True
    host_path = re.sub(r"^https?://(www\.|m\.)?", "", low)
    if any(host_path.startswith(d) for d in SOCIAL_DOMAINS):
        return SourceType.SOCIAL_MEDIA, ContentKind.SOCIAL_POST, False
    if any(host_path.startswith(d) for d in COMMUNITY_DOMAINS):
        return SourceType.COMMUNITY, ContentKind.COMMUNITY_POST, False
    if default == SourceType.SOCIAL_MEDIA:
        return default, ContentKind.SOCIAL_POST, False
    if default == SourceType.COMMUNITY:
        return default, ContentKind.COMMUNITY_POST, False
    return default, ContentKind.SEARCH_RESULT, False


def hits_to_discoveries(hits: list[SearchHit], query: QuerySpec, default_type: SourceType, source_name: str,
                        ctx: DiscoveryContext) -> list[RawDiscovery]:
    now = utc_now()
    out: list[RawDiscovery] = []
    for h in hits:
        ats = detect_ats(h.url)
        if ats:
            ctx.add_ats_token(*ats)
        stype, kind, official = classify_url(h.url, default_type)
        published = h.published_hint
        snippet = h.description + (" " + " ".join(h.extra_snippets) if h.extra_snippets else "")
        d = RawDiscovery(
            source_type=stype,
            source_name=source_name,
            source_url=h.url,
            title=re.sub(r"<[^>]+>", "", h.title),
            retrieved_at=now,
            snippet=re.sub(r"<[^>]+>", "", snippet),
            content_kind=kind,
            official_channel=official,
            query=query.text,
            language=query.language,
            country=query.country,
        )
        # Search-engine dates describe the indexed page, not necessarily the job: keep as a hint only.
        if published:
            if kind == ContentKind.SOCIAL_POST:
                d.post_created_at = published
            d.published_at = published
            d.published_at_evidence = f"search index date ({h.page_age or h.age_text})"
        d.raw = {"search_provider": h.provider, "rank": h.rank, "domain": domain_of(h.url)}
        out.append(d)
    return out


class SearchBackedSource(BaseJobSource):
    uses_search_provider = True

    def source_label(self, query: QuerySpec) -> str:
        return query.purpose.split(":", 1)[1] if ":" in query.purpose else self.name

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:
        assert ctx.search is not None
        hits = await ctx.search.search(query)
        return hits_to_discoveries(hits, query, self.category, self.source_label(query), ctx)


class WebSearchSource(SearchBackedSource):
    name = "Web Search"
    category = SourceType.SEARCH_ENGINE
    description = "Targeted web search queries (hiring phrases x roles x countries)"

    def source_label(self, query: QuerySpec) -> str:
        return "Web Search"
