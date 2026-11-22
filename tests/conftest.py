"""Shared test fixtures. The default suite never touches the network or real credentials."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from src.core.configuration import Credentials, Settings, load_settings

FIXTURES = Path(__file__).parent / "fixtures"
ENV_KEYS = ["AI_PROVIDER", "AI_MODEL", "GEMINI_API_KEY", "SEARCH_PROVIDER", "SEARCH_API_KEY",
            "SECONDARY_SEARCH_PROVIDER", "SECONDARY_SEARCH_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
            "X_BEARER_TOKEN"]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    s = load_settings(load_env=False)
    s.raw["paths"] = {
        "database": str(tmp_path / "db" / "test.sqlite3"),
        "cache_directory": str(tmp_path / "cache"),
        "log_directory": str(tmp_path / "logs"),
        "preferences_file": str(tmp_path / "prefs.json"),
    }
    s.raw["reporting"]["output_directory"] = str(tmp_path / "reports")
    perf = s.raw["performance"]
    perf["default_host_interval_seconds"] = 0
    perf["host_intervals"] = {}
    perf["retry_attempts"] = 1
    perf["backoff_base_seconds"] = 0.01
    perf["cache_ttl_hours"] = {"default": 0}
    return s


def with_credentials(s: Settings, **kw: str) -> Settings:
    return dataclasses.replace(s, credentials=dataclasses.replace(s.credentials, **kw))


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class Router:
    """httpx MockTransport router: (host, path prefix) -> handler. Unknown routes return 404."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.requests: list[httpx.Request] = []

    def add(self, host: str, path_prefix: str, handler: Callable[[httpx.Request], httpx.Response] | Any) -> "Router":
        if not callable(handler):
            payload = handler
            handler = lambda req, p=payload: _respond(p)  # noqa: E731
        self.routes.append((host, path_prefix, handler))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for host, prefix, handler in self.routes:
            if request.url.host == host and request.url.path.startswith(prefix):
                return handler(request)
        return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def hits(self, host: str) -> int:
        return sum(1 for r in self.requests if r.url.host == host)


def _respond(payload: Any) -> httpx.Response:
    if isinstance(payload, httpx.Response):
        return payload
    if isinstance(payload, str):
        return httpx.Response(200, text=payload, headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(200, json=payload)


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def sample_cv_path() -> Path:
    return FIXTURES / "sample_cv.txt"


def live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_TESTS") == "1"
