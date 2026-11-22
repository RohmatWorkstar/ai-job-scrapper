from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.exceptions import UnsafeURLError
from src.utils.date_time import folder_timestamp, format_local, parse_datetime, to_local, within_window
from src.utils.http_client import SafeHttpClient
from src.utils.logger import redact, register_secrets
from src.utils.validators import canonical_url, validate_url_safe, validate_url_syntax
from tests.conftest import public_resolver

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ dates
def test_parse_datetime_variants() -> None:
    assert parse_datetime("2026-11-19T09:00:00-05:00") == datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    assert parse_datetime(1795000000000) == datetime.fromtimestamp(1795000000, tz=timezone.utc)
    assert parse_datetime("1795000000") == datetime.fromtimestamp(1795000000, tz=timezone.utc)
    assert parse_datetime("3 days ago", now=NOW) == NOW - timedelta(days=3)
    assert parse_datetime("2 hari yang lalu", now=NOW) == NOW - timedelta(days=2)
    assert parse_datetime("yesterday", now=NOW) == NOW - timedelta(days=1)
    assert parse_datetime("November 20, 2026", now=NOW).date().isoformat() == "2026-11-20"
    assert parse_datetime("not a date") is None
    assert parse_datetime("2030-01-01", now=NOW) is None  # implausible future date
    assert parse_datetime(None) is None


def test_jakarta_timezone() -> None:
    dt = datetime(2026, 11, 22, 13, 35, 0, tzinfo=timezone.utc)
    loc = to_local(dt, "Asia/Jakarta")
    assert loc.utcoffset() == timedelta(hours=7)
    assert format_local(dt) == "2026-11-22 20:35"
    assert folder_timestamp(dt) == "2026-11-22_203500"
    naive = datetime(2026, 1, 1, 0, 0)  # treated as UTC
    assert format_local(naive) == "2026-01-01 07:00"


def test_within_window() -> None:
    assert within_window(NOW - timedelta(days=2), 7, NOW) is True
    assert within_window(NOW - timedelta(days=20), 7, NOW) is False
    assert within_window(None, 7, NOW) is None  # unknown is never guessed
    assert within_window(None, None, NOW) is True


# ------------------------------------------------------------- validators
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://x.com/a", "http://localhost/admin", "http://127.0.0.1/", "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://user:pass@example.com/", "javascript:alert(1)",
    "http://printer.local/",
])
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_url_syntax(url)


def test_dns_rebinding_to_private_rejected() -> None:
    async def private(host: str) -> list[str]:
        return ["192.168.1.10"]

    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url_safe("https://evil.example/", private))
    assert asyncio.run(validate_url_safe("https://ok.example/", public_resolver)) == "https://ok.example/"


def test_canonical_url() -> None:
    a = canonical_url("https://www.Example.com/jobs/123/?utm_source=x&gh_src=abc&id=5#apply")
    b = canonical_url("http://example.com/jobs/123?id=5")
    assert a == b == "https://example.com/jobs/123?id=5"


def test_log_redaction() -> None:
    register_secrets(["SuperSecretKey123"])
    out = redact("key=SuperSecretKey123 Bearer abc.def email me@x.com phone +62 812 3456 7890 ?api_key=zzz")
    assert "SuperSecretKey123" not in out and "me@x.com" not in out and "zzz" not in out and "abc.def" not in out


# ------------------------------------------------------------ http client
def _client(handler, **kw) -> SafeHttpClient:
    async def nosleep(_: float) -> None:
        return None

    return SafeHttpClient(user_agent="test", retry_attempts=kw.pop("retries", 2), default_host_interval=0,
                          transport=httpx.MockTransport(handler), resolver=public_resolver, sleep=nosleep, **kw)


def test_retry_then_success() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 2 else httpx.Response(200, json={"ok": True})

    async def go():
        async with _client(handler) as c:
            return await c.get("https://api.example/x", respect_robots=False)

    res = asyncio.run(go())
    assert res.ok and res.json() == {"ok": True} and calls["n"] == 2


def test_no_retry_on_permanent_errors() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    async def go():
        async with _client(handler) as c:
            return await c.get("https://api.example/x", respect_robots=False)

    res = asyncio.run(go())
    assert not res.ok and res.status == 401 and calls["n"] == 1


def test_robots_txt_respected() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", headers={"content-type": "text/plain"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    async def go():
        async with _client(handler) as c:
            return await c.get("https://site.example/private/job"), await c.get("https://site.example/jobs/1")

    blocked, allowed = asyncio.run(go())
    assert blocked.error_kind == "blocked_robots" and allowed.ok


def test_redirect_to_private_address_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async def go():
        async with _client(handler) as c:
            return await c.get("https://site.example/r", respect_robots=False)

    res = asyncio.run(go())
    assert res.error_kind == "unsafe_url"


def test_binary_downloads_refused_and_size_capped() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
        return httpx.Response(200, text="x" * 5000, headers={"content-type": "text/html"})

    async def go():
        async with _client(handler, max_bytes=1000) as c:
            return await c.get("https://s.example/f.pdf", respect_robots=False), await c.get("https://s.example/big", respect_robots=False)

    pdf, big = asyncio.run(go())
    assert pdf.error_kind == "content_type" and big.error_kind == "too_large"


def test_cache_reuse(tmp_path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    async def go():
        async with _client(handler, cache_dir=tmp_path, cache_ttl_hours={"default": 1}) as c:
            a = await c.get("https://feed.example/api", respect_robots=False, use_cache=True)
            b = await c.get("https://feed.example/api", respect_robots=False, use_cache=True)
            return a, b

    a, b = asyncio.run(go())
    assert calls["n"] == 1 and b.from_cache and b.json() == {"n": 1}
