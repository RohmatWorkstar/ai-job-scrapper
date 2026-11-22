"""Policy-enforcing async HTTP client shared by all sources.

Guarantees for every request:
* URL validation + DNS check against private/loopback/link-local ranges (SSRF),
  re-validated on every redirect hop (redirects are followed manually).
* Optional robots.txt compliance (always on for HTML page fetches).
* Bounded global concurrency + per-host minimum interval (politeness / rate limits).
* Timeouts, bounded retries with exponential backoff for transient failures only
  (never for 401/403/404/410 or policy blocks).
* Content-type allow-list and response size cap: files are never downloaded.
* Optional on-disk JSON cache with per-host TTL (several feeds ask for few calls/day).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from src.core.exceptions import UnsafeURLError
from src.utils.logger import get_logger, log_event
from src.utils.retry import backoff_delay
from src.utils.validators import Resolver, validate_url_safe

log = get_logger(__name__)

TEXT_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "application/json", "text/plain", "application/ld+json",
                      "text/xml", "application/xml", "application/rss+xml", "application/atom+xml")
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_REDIRECTS = 5


@dataclass
class HttpResult:
    url: str
    final_url: str = ""
    status: int | None = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None
    error_kind: str | None = None  # network|timeout|http|blocked_robots|unsafe_url|content_type|too_large|policy
    from_cache: bool = False
    elapsed_ms: int = 0
    _json: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    def json(self) -> Any:
        if self._json is None:
            self._json = json.loads(self.text)
        return self._json


class SafeHttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        max_concurrency: int = 5,
        retry_attempts: int = 2,
        backoff_base: float = 1.5,
        max_bytes: int = 4_000_000,
        default_host_interval: float = 1.0,
        host_intervals: Mapping[str, float] | None = None,
        cache_dir: Path | None = None,
        cache_ttl_hours: Mapping[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.backoff_base = backoff_base
        self.max_bytes = max_bytes
        self.default_host_interval = default_host_interval
        self.host_intervals = dict(host_intervals or {})
        self.cache_dir = cache_dir
        self.cache_ttl_hours = dict(cache_ttl_hours or {})
        self._sem = asyncio.Semaphore(max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._resolver = resolver
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept-Language": "en,id;q=0.8"},
            transport=transport,
            limits=httpx.Limits(max_connections=max_concurrency * 2, max_keepalive_connections=max_concurrency),
        )
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SafeHttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ cache
    def _ttl_for(self, host: str) -> float:
        return float(self.cache_ttl_hours.get(host, self.cache_ttl_hours.get("default", 0)))

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / "http" / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _cache_get(self, key: str, ttl_hours: float) -> HttpResult | None:
        path = self._cache_path(key)
        if not path or ttl_hours <= 0 or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data["stored"] > ttl_hours * 3600:
                return None
            return HttpResult(url=data["url"], final_url=data["final_url"], status=data["status"],
                              text=data["text"], headers=data.get("headers", {}), from_cache=True)
        except (OSError, ValueError, KeyError):
            return None

    def _cache_put(self, key: str, res: HttpResult) -> None:
        path = self._cache_path(key)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_headers = {k: v for k, v in res.headers.items() if k.lower() in {"content-type", "last-modified", "date"}}
            path.write_text(json.dumps({"stored": time.time(), "url": res.url, "final_url": res.final_url,
                                        "status": res.status, "text": res.text, "headers": safe_headers}),
                            encoding="utf-8")
        except OSError:
            pass

    # --------------------------------------------------------------- throttle
    async def _throttle(self, host: str) -> None:
        interval = self.host_intervals.get(host, self.default_host_interval)
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._host_last[host] = time.monotonic()

    # ----------------------------------------------------------------- robots
    async def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        res = await self._request("GET", origin + "/robots.txt", accept_any_text=True, retries=0)
        parser = RobotFileParser()
        if res.status in (401, 403):
            parser.parse(["User-agent: *", "Disallow: /"])  # access-restricted robots => assume disallow
            return parser
        if not res.ok:
            return None  # no robots.txt (404) or unreachable: no restrictions declared
        parser.parse(res.text.splitlines())
        return parser

    # ------------------------------------------------------------------ public
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        respect_robots: bool = True,
        use_cache: bool = False,
        cache_ttl_hours: float | None = None,
        retries: int | None = None,
        max_bytes: int | None = None,
    ) -> HttpResult:
        full_url =str(httpx.URL(url, params={k: v for k, v in (params or {}).items() if v is not None})) if params else url
        host = urlsplit(full_url).hostname or ""
        ttl = cache_ttl_hours if cache_ttl_hours is not None else self._ttl_for(host)
        cache_key = full_url + "|" + json.dumps(sorted((k, v) for k, v in (headers or {}).items() if k.lower() == "accept"))
        if use_cache:
            cached = self._cache_get(cache_key, ttl)
            if cached:
                self.stats["cache_hits"] += 1
                return cached
        if respect_robots:
            try:
                allowed = await self.robots_allowed(full_url)
            except Exception:  # noqa: BLE001 - robots failures must not crash a source
                allowed = True
            if not allowed:
                self.stats["robots_blocked"] += 1
                return HttpResult(url=full_url, error="Disallowed by robots.txt", error_kind="blocked_robots")
        res = await self._request("GET", full_url, headers=headers, retries=retries, max_bytes=max_bytes)
        if use_cache and res.ok:
            self._cache_put(cache_key, res)
        return res

    async def post(
        self,
        url: str,
        *,
        json_body: Any = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        retries: int | None = None,
    ) -> HttpResult:
        return await self._request("POST", url, headers=headers, json_body=json_body, data=data, auth=auth,
                                   retries=retries)

    # ---------------------------------------------------------------- engine
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        retries: int | None = None,
        accept_any_text: bool = False,
        max_bytes: int | None = None,
    ) -> HttpResult:
        attempts = self.retry_attempts if retries is None else retries
        result = HttpResult(url=url)
        for attempt in range(attempts + 1):
            result = await self._single(method, url, headers, json_body, data, auth, accept_any_text,
                                        max_bytes or self.max_bytes)
            retryable = (result.error_kind in {"network", "timeout"}) or (result.status in RETRYABLE_STATUS)
            if not retryable or attempt >= attempts:
                break
            retry_after = None
            ra = result.headers.get("retry-after") if result.headers else None
            if ra and ra.strip().isdigit():
                retry_after = float(ra.strip())
            delay = backoff_delay(attempt + 1, self.backoff_base, cap=30.0, retry_after=retry_after)
            log_event(log, 20, "http.retry", url=url, status=result.status, kind=result.error_kind, delay=round(delay, 2))
            await self._sleep(delay)
        if not result.ok:
            self.stats["errors"] += 1
        return result

    async def _single(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        json_body: Any,
        data: Mapping[str, str] | None,
        auth: tuple[str, str] | None,
        accept_any_text: bool,
        max_bytes: int,
    ) -> HttpResult:
        started = time.monotonic()
        current = url
        req_headers = dict(headers or {})
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            req_headers["Authorization"] = f"Basic {token}"
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                await validate_url_safe(current, self._resolver)
            except UnsafeURLError as exc:
                return HttpResult(url=url, final_url=current, error=str(exc), error_kind="unsafe_url")
            host = urlsplit(current).hostname or ""
            if _hop > 0 and urlsplit(current).hostname != urlsplit(url).hostname:
                req_headers.pop("Authorization", None)  # never forward credentials cross-host
            await self._throttle(host)  # outside the semaphore: slow hosts don't starve others
            async with self._sem:
                self.stats["requests"] += 1
                try:
                    req = self._client.build_request(method, current, headers=req_headers, json=json_body, data=data)
                    resp = await self._client.send(req, stream=True)
                except httpx.TimeoutException:
                    return HttpResult(url=url, final_url=current, error="Request timed out", error_kind="timeout",
                                      elapsed_ms=int((time.monotonic() - started) * 1000))
                except httpx.HTTPError as exc:
                    return HttpResult(url=url, final_url=current, error=f"Network error: {type(exc).__name__}",
                                      error_kind="network", elapsed_ms=int((time.monotonic() - started) * 1000))
                try:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return HttpResult(url=url, final_url=current, status=resp.status_code,
                                              error="Redirect without location", error_kind="http")
                        current = urljoin(current, location)
                        if method == "POST" and resp.status_code in (301, 302, 303):
                            method, json_body, data = "GET", None, None
                        continue
                    return await self._read_body(url, current, resp, started, accept_any_text, max_bytes)
                finally:
                    await resp.aclose()
        return HttpResult(url=url, final_url=current, error="Too many redirects", error_kind="http")

    async def _read_body(self, url: str, final_url: str, resp: httpx.Response, started: float,
                         accept_any_text: bool, max_bytes: int) -> HttpResult:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        elapsed = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        if ctype and not accept_any_text and not any(ctype.startswith(t) for t in TEXT_CONTENT_TYPES):
            return HttpResult(url=url, final_url=final_url, status=resp.status_code, headers=headers,
                              error=f"Content type not processed: {ctype}", error_kind="content_type", elapsed_ms=elapsed())
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return HttpResult(url=url, final_url=final_url, status=resp.status_code, headers=headers,
                              error="Response too large", error_kind="too_large", elapsed_ms=elapsed())
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return HttpResult(url=url, final_url=final_url, status=resp.status_code, headers=headers,
                                      error="Response too large", error_kind="too_large", elapsed_ms=elapsed())
                chunks.append(chunk)
        except httpx.TimeoutException:
            return HttpResult(url=url, final_url=final_url, status=resp.status_code, error="Read timed out",
                              error_kind="timeout", elapsed_ms=elapsed())
        except httpx.HTTPError as exc:
            return HttpResult(url=url, final_url=final_url, status=resp.status_code,
                              error=f"Network error: {type(exc).__name__}", error_kind="network", elapsed_ms=elapsed())
        raw = b"".join(chunks)
        encoding = resp.charset_encoding or "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        result = HttpResult(url=url, final_url=final_url, status=resp.status_code, text=text, headers=headers,
                            elapsed_ms=elapsed())
        if not 200 <= resp.status_code < 300:
            result.error = f"HTTP {resp.status_code}"
            result.error_kind = "http"
        return result
