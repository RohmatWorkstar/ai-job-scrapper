"""Reddit hiring communities via the official Reddit Data API (application-only OAuth).

Token: POST https://www.reddit.com/api/v1/access_token  (grant_type=client_credentials, HTTP Basic auth)
Search: GET https://oauth.reddit.com/r/<subs>/search?q=&restrict_sr=1&sort=new&t=week&limit=
Unauthenticated scraping of reddit.com is not used. Without credentials, Reddit is
still covered through indexed public pages (Communities (indexed) source).
"""

from __future__ import annotations

import time

from src.core.exceptions import AuthenticationError
from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceType
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

SUBREDDITS = ["forhire", "remotejs", "jobbit", "hiring", "RemoteJobs", "devopsjobs", "PythonJobs", "reactjs"]


def reddit_time_filter(days: int | None) -> str:
    if days is None:
        return "all"
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


class RedditSource(BaseJobSource):
    name = "Reddit (API)"
    category = SourceType.COMMUNITY
    description = "Hiring posts in job subreddits via the official Reddit API"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self._token: str | None = None
        self._token_exp = 0.0

    def is_configured(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        c = ctx.settings.credentials
        if not (c.reddit_client_id and c.reddit_client_secret):
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"
        return True, ""

    def build_queries(self, ctx: DiscoveryContext) -> list[QuerySpec]:
        return [QuerySpec(text=kw, category=self.category, purpose="community:Reddit API",
                          freshness_days=ctx.config.publication_window_days) for kw in ctx.plan.keywords[:4]]

    async def _auth(self, ctx: DiscoveryContext) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        c = ctx.settings.credentials
        res = await ctx.http.post("https://www.reddit.com/api/v1/access_token",
                                  data={"grant_type": "client_credentials"},
                                  auth=(c.reddit_client_id, c.reddit_client_secret))
        if res.status in (401, 403):
            raise AuthenticationError("Reddit rejected the client credentials")
        if not res.ok:
            raise http_error_to_exception(self.name, res)
        data = res.json()
        if "access_token" not in data:
            raise AuthenticationError("Reddit did not return an access token")
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 3600))
        return self._token

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:
        token = await self._auth(ctx)
        subs = "+".join(SUBREDDITS)
        res = await ctx.http.get(f"https://oauth.reddit.com/r/{subs}/search",
                                 params={"q": f"hiring {query.text}", "restrict_sr": 1, "sort": "new",
                                         "t": reddit_time_filter(query.freshness_days), "limit": 50},
                                 headers={"Authorization": f"bearer {token}"}, respect_robots=False)
        if not res.ok:
            raise http_error_to_exception(self.name, res)
        out = []
        now = utc_now()
        for child in (res.json().get("data") or {}).get("children", []):
            p = child.get("data") or {}
            title = p.get("title") or ""
            low = title.lower()
            if "[for hire]" in low or "for hire" in (p.get("link_flair_text") or "").lower():
                continue  # people looking for work, not openings
            created = parse_datetime(p.get("created_utc"))
            out.append(RawDiscovery(
                source_type=self.category, source_name=f"Reddit r/{p.get('subreddit')}",
                source_url=f"https://www.reddit.com{p.get('permalink', '')}", title=title[:200], retrieved_at=now,
                text=(p.get("selftext") or "")[:40000], snippet=(p.get("selftext") or "")[:300],
                published_at=created, post_created_at=created, published_at_evidence="Reddit created_utc (post date)",
                content_kind=ContentKind.COMMUNITY_POST, query=query.text, external_id=f"reddit:{p.get('id')}",
            ))
        return out
