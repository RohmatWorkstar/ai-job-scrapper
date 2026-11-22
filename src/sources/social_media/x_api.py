"""X (Twitter) API v2 recent search - official API, requires a paid access tier.

GET https://api.x.com/2/tweets/search/recent
    query, max_results (10-100), tweet.fields, expansions=author_id, user.fields
Auth: ``Authorization: Bearer <X_BEARER_TOKEN>``. Only public posts are returned.
"""

from __future__ import annotations

from src.core.exceptions import AuthenticationError
from src.core.models import ContentKind, QuerySpec, RawDiscovery, SourceType
from src.sources.base import BaseJobSource, DiscoveryContext, http_error_to_exception
from src.utils.date_time import parse_datetime, utc_now

ENDPOINT = "https://api.x.com/2/tweets/search/recent"


class XRecentSearchSource(BaseJobSource):
    name = "X API (recent search)"
    category = SourceType.SOCIAL_MEDIA
    description = "Public posts from the last 7 days via the official X API v2"

    def is_configured(self, ctx: DiscoveryContext) -> tuple[bool, str]:
        if not ctx.settings.credentials.x_bearer_token:
            return False, "X_BEARER_TOKEN not set (X API access is a paid tier)"
        return True, ""

    def build_queries(self, ctx: DiscoveryContext) -> list[QuerySpec]:
        out = []
        for role in ctx.plan.role_terms[:3]:
            text = f'("we are hiring" OR "we\'re hiring" OR "now hiring" OR lowongan) "{role}" -is:retweet'
            out.append(QuerySpec(text=text, category=self.category, purpose="social:X API", role=role))
        return out

    async def search(self, query: QuerySpec, ctx: DiscoveryContext) -> list[RawDiscovery]:
        token = ctx.settings.credentials.x_bearer_token
        params = {"query": query.text[:512], "max_results": 50, "tweet.fields": "created_at,lang,author_id",
                  "expansions": "author_id", "user.fields": "username,name"}
        res = await ctx.http.get(ENDPOINT, params=params, respect_robots=False,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if res.status in (401, 403):
            raise AuthenticationError(f"X API rejected the bearer token or access tier (HTTP {res.status})")
        if not res.ok:
            raise http_error_to_exception(self.name, res)
        data = res.json()
        users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
        out = []
        now = utc_now()
        for t in data.get("data", []) or []:
            user = users.get(t.get("author_id"), {})
            handle = user.get("username") or "i"
            created = parse_datetime(t.get("created_at"))
            text = t.get("text", "")
            out.append(RawDiscovery(
                source_type=self.category, source_name="X / Twitter (API)",
                source_url=f"https://x.com/{handle}/status/{t['id']}", title=text.split("\n")[0][:140],
                retrieved_at=now, text=text, snippet=text[:300], post_created_at=created, published_at=created,
                published_at_evidence="X API created_at (post date)", content_kind=ContentKind.SOCIAL_POST,
                query=query.text, language=t.get("lang"), external_id=f"x:{t['id']}",
                raw={"author": user.get("name"), "handle": handle},
            ))
        return out
