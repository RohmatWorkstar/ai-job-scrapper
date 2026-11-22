"""Configuration loading: YAML settings + environment variables (.env).

Credentials are read only from the environment and are exposed through
:class:`Credentials`, whose ``__repr__`` never prints secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.core.exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Credentials:
    ai_provider: str = "gemini"
    ai_model: str = ""
    gemini_api_key: str = ""
    search_provider: str = ""
    search_api_key: str = ""
    secondary_search_provider: str = ""
    secondary_search_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    x_bearer_token: str = ""

    def __repr__(self) -> str:  # never leak secrets through logging/debugging
        def mask(v: str) -> str:
            return "<set>" if v else "<empty>"

        return (
            f"Credentials(ai_provider={self.ai_provider!r}, ai_model={self.ai_model!r}, "
            f"gemini_api_key={mask(self.gemini_api_key)}, search_provider={self.search_provider!r}, "
            f"search_api_key={mask(self.search_api_key)}, "
            f"secondary_search_provider={self.secondary_search_provider!r}, "
            f"secondary_search_api_key={mask(self.secondary_search_api_key)}, "
            f"reddit={mask(self.reddit_client_id)}, x={mask(self.x_bearer_token)})"
        )

    __str__ = __repr__

    def secret_values(self) -> list[str]:
        return [
            v
            for v in (
                self.gemini_api_key,
                self.search_api_key,
                self.secondary_search_api_key,
                self.reddit_client_secret,
                self.reddit_client_id,
                self.x_bearer_token,
            )
            if v and len(v) >= 6
        ]

    @property
    def has_ai(self) -> bool:
        return bool(self.gemini_api_key) and self.ai_provider.lower() == "gemini"

    @property
    def has_search(self) -> bool:
        return bool(self.search_api_key and self.search_provider) or bool(
            self.secondary_search_api_key and self.secondary_search_provider
        )

    @classmethod
    def from_env(cls) -> "Credentials":
        g = lambda k: (os.environ.get(k) or "").strip()  # noqa: E731
        return cls(
            ai_provider=g("AI_PROVIDER") or "gemini",
            ai_model=g("AI_MODEL"),
            gemini_api_key=g("GEMINI_API_KEY"),
            search_provider=(g("SEARCH_PROVIDER") or ("brave" if g("SEARCH_API_KEY") else "")).lower(),
            search_api_key=g("SEARCH_API_KEY"),
            secondary_search_provider=g("SECONDARY_SEARCH_PROVIDER").lower(),
            secondary_search_api_key=g("SECONDARY_SEARCH_API_KEY"),
            reddit_client_id=g("REDDIT_CLIENT_ID"),
            reddit_client_secret=g("REDDIT_CLIENT_SECRET"),
            x_bearer_token=g("X_BEARER_TOKEN"),
        )


@dataclass
class Settings:
    raw: dict[str, Any]
    profiles: dict[str, Any]
    taxonomy: dict[str, Any]
    credentials: Credentials
    root: Path = field(default=PROJECT_ROOT)

    # ---- generic accessors -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str) -> Path:
        p = Path(self.get(dotted, default))
        return p if p.is_absolute() else self.root / p

    # ---- frequently used values -------------------------------------------
    @property
    def timezone(self) -> str:
        return str(self.get("application.timezone", "Asia/Jakarta"))

    @property
    def user_agent(self) -> str:
        return str(self.get("application.user_agent", "AIJobIntelligence/1.0"))

    @property
    def cv_directory(self) -> Path:
        return self.path("candidate.cv_directory", "cv")

    @property
    def database_path(self) -> Path:
        return self.path("paths.database", "data/database/job_intelligence.sqlite3")

    @property
    def cache_directory(self) -> Path:
        return self.path("paths.cache_directory", "data/cache")

    @property
    def log_directory(self) -> Path:
        return self.path("paths.log_directory", "data/logs")

    @property
    def preferences_file(self) -> Path:
        return self.path("paths.preferences_file", "data/preferences.json")

    @property
    def reports_directory(self) -> Path:
        return self.path("reporting.output_directory", "reports")

    def country_table(self) -> dict[str, dict[str, Any]]:
        return dict(self.profiles.get("countries") or {})


def _load_yaml(path: Path, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigurationError(f"Configuration file not found: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path.name} must contain a mapping at top level")
    return data


def _validate(raw: dict[str, Any]) -> None:
    weights = (raw.get("matching") or {}).get("weights") or {}
    if weights:
        try:
            total = sum(float(v) for v in weights.values())
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("matching.weights values must be numbers") from exc
        if total <= 0:
            raise ConfigurationError("matching.weights must sum to a positive number")
    perf = raw.get("performance") or {}
    conc = perf.get("max_concurrency", 5)
    if not isinstance(conc, int) or not 1 <= conc <= 50:
        raise ConfigurationError("performance.max_concurrency must be an integer between 1 and 50")
    timeout = perf.get("request_timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigurationError("performance.request_timeout_seconds must be positive")
    retries = perf.get("retry_attempts", 2)
    if not isinstance(retries, int) or not 0 <= retries <= 5:
        raise ConfigurationError("performance.retry_attempts must be between 0 and 5")


def load_settings(
    config_dir: Path | None = None,
    env_file: Path | None = None,
    *,
    load_env: bool = True,
) -> Settings:
    """Load settings.yaml, search_profiles.yaml, skills_taxonomy.yaml and .env."""
    config_dir = config_dir or PROJECT_ROOT / "config"
    if load_env:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    raw = _load_yaml(config_dir / "settings.yaml", required=True)
    _validate(raw)
    profiles = _load_yaml(config_dir / "search_profiles.yaml", required=False)
    taxonomy = _load_yaml(config_dir / "skills_taxonomy.yaml", required=False)
    return Settings(raw=raw, profiles=profiles, taxonomy=taxonomy, credentials=Credentials.from_env())
