"""AI provider manager: provider selection, per-run budget, response cache, graceful degradation.

Callers receive ``None`` whenever AI is unavailable, over budget or fails; they
must then fall back to deterministic processing.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from src.ai.base import AIProvider
from src.ai.gemini_provider import GeminiProvider
from src.ai.prompts import SYSTEM_GUARD
from src.core.configuration import Settings
from src.core.exceptions import AIBudgetExceeded, AIProviderError
from src.utils.http_client import SafeHttpClient
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)

PROVIDERS: dict[str, type[GeminiProvider]] = {"gemini": GeminiProvider}


class AIManager:
    def __init__(
        self,
        provider: AIProvider | None,
        *,
        max_calls: int = 25,
        cache_dir: Path | None = None,
        cache_enabled: bool = True,
        disabled_reason: str = "",
    ) -> None:
        self.provider = provider
        self.max_calls = max_calls
        self.calls = 0
        self.cache_hits = 0
        self.errors: list[str] = []
        self.cache_dir = cache_dir
        self.cache_enabled = cache_enabled
        self.disabled_reason = disabled_reason if provider is None else ""
        self._prepared = False

    # ------------------------------------------------------------------ build
    @classmethod
    def from_settings(cls, settings: Settings, http: SafeHttpClient | None) -> "AIManager":
        creds = settings.credentials
        cache_dir = settings.cache_directory / "ai"
        max_calls = int(settings.get("ai.max_calls_per_run", 25))
        if not settings.get("ai.enabled", True):
            return cls(None, disabled_reason="AI disabled in config/settings.yaml", cache_dir=cache_dir)
        provider_name = (creds.ai_provider or "gemini").lower()
        if provider_name not in PROVIDERS:
            return cls(None, disabled_reason=f"AI provider '{provider_name}' is not supported", cache_dir=cache_dir)
        if not creds.gemini_api_key:
            return cls(None, disabled_reason="GEMINI_API_KEY not set in .env", cache_dir=cache_dir)
        if http is None:
            return cls(None, disabled_reason="No HTTP client", cache_dir=cache_dir)
        provider = PROVIDERS[provider_name](
            http, creds.gemini_api_key, creds.ai_model, str(settings.get("ai.model_preference", "flash"))
        )
        return cls(provider, max_calls=max_calls, cache_dir=cache_dir,
                   cache_enabled=bool(settings.get("ai.cache_responses", True)))

    # ----------------------------------------------------------------- state
    @property
    def available(self) -> bool:
        return self.provider is not None and not self.disabled_reason

    @property
    def model(self) -> str:
        return self.provider.model if self.provider else ""

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    def status_line(self) -> str:
        if not self.available:
            return f"disabled ({self.disabled_reason})"
        return f"{self.provider.name}:{self.model or 'auto'} - {self.calls}/{self.max_calls} calls, {self.cache_hits} cached"

    async def prepare(self) -> bool:
        if not self.available:
            return False
        if self._prepared:
            return True
        try:
            await self.provider.prepare()  # type: ignore[union-attr]
            self._prepared = True
            return True
        except AIProviderError as exc:
            self._disable(str(exc))
            return False

    def _disable(self, reason: str) -> None:
        self.disabled_reason = reason
        self.errors.append(reason)
        log_event(log, 30, "ai.disabled", reason=reason)

    # ----------------------------------------------------------------- cache
    def _cache_path(self, key: str) -> Path | None:
        if not (self.cache_enabled and self.cache_dir):
            return None
        return self.cache_dir / f"{key}.json"

    def _cache_get(self, key: str, max_age_days: float) -> Any:
        p = self._cache_path(key)
        if not p or not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - data["stored"] > max_age_days * 86400:
                return None
            return data["response"]
        except (OSError, ValueError, KeyError):
            return None

    def _cache_put(self, key: str, response: Any) -> None:
        p = self._cache_path(key)
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"stored": time.time(), "response": response}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ call
    async def ask_json(self, task: str, prompt: str, *, cache_days: float = 7.0, max_output_tokens: int = 8192) -> Any:
        if not self.available or not await self.prepare():
            return None
        # Model is resolved before keying the cache so the same prompt always maps to one entry.
        key = hashlib.sha256(f"{task}|{self.model}|{prompt}".encode()).hexdigest()
        cached = self._cache_get(key, cache_days)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self.calls >= self.max_calls:
            self.errors.append(str(AIBudgetExceeded(f"AI call budget ({self.max_calls}) exhausted; '{task}' skipped")))
            return None
        self.calls += 1
        try:
            result = await self.provider.generate_json(SYSTEM_GUARD, prompt, max_output_tokens=max_output_tokens)  # type: ignore[union-attr]
        except AIProviderError as exc:
            msg = f"{task}: {exc}"
            self.errors.append(msg)
            log_event(log, 30, "ai.error", task=task, error=str(exc), permanent=exc.permanent)
            if exc.permanent:
                self._disable(str(exc))
            return None
        self._cache_put(key, result)
        return result
