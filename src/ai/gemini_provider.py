"""Google Gemini provider using the documented REST API (Gemini API v1beta).

Endpoints (https://ai.google.dev/api):
* ``GET  /v1beta/models``                          - models.list (pageSize, pageToken)
* ``POST /v1beta/models/{model}:generateContent``  - contents/systemInstruction/generationConfig

When ``AI_MODEL`` is empty the newest stable model of the preferred family
(``flash`` or ``flash-lite``) is resolved from models.list, so no model name is
hard-coded and deprecated names are never pinned.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.ai.base import AIProvider
from src.core.exceptions import AIProviderError
from src.utils.http_client import SafeHttpClient
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_STABLE_RE = {
    "flash": re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$"),
    "flash-lite": re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash-lite$"),
}
_LATEST_ALIAS = {"flash": "gemini-flash-latest", "flash-lite": "gemini-flash-lite-latest"}


def choose_model(model_ids: list[str], preference: str = "flash") -> str | None:
    """Pick the newest stable model of the requested family from a models.list result."""
    preference = preference if preference in _STABLE_RE else "flash"
    pattern = _STABLE_RE[preference]
    versions: list[tuple[tuple[int, ...], str]] = []
    for mid in model_ids:
        m = pattern.match(mid)
        if m:
            versions.append((tuple(int(x) for x in m.group(1).split(".")), mid))
    if versions:
        return max(versions)[1]
    alias = _LATEST_ALIAS[preference]
    if alias in model_ids:
        return alias
    other = "flash-lite" if preference == "flash" else "flash"
    for mid in model_ids:
        if _STABLE_RE[other].match(mid):
            return choose_model(model_ids, other)
    return None


def extract_json(text: str) -> Any:
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
        if start >= 0:
            end = max(t.rfind("}"), t.rfind("]"))
            if end > start:
                try:
                    return json.loads(t[start : end + 1])
                except json.JSONDecodeError:
                    pass
    raise AIProviderError("AI response was not valid JSON")


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, http: SafeHttpClient, api_key: str, model: str = "", preference: str = "flash") -> None:
        if not api_key:
            raise AIProviderError("GEMINI_API_KEY is not set", permanent=True)
        self._http = http
        self._key = api_key
        self._model = model.strip().removeprefix("models/")
        self._preference = preference

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    @staticmethod
    def _raise_for(res: Any, action: str) -> None:
        if res.ok:
            return
        status = res.status
        if status in (401, 403):
            raise AIProviderError(f"Gemini rejected the API key during {action} (HTTP {status})", permanent=True)
        if status == 400:
            detail = ""
            try:
                detail = res.json().get("error", {}).get("message", "")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise AIProviderError(f"Gemini {action} failed (HTTP 400): {detail}", permanent=True)
        if status == 404:
            raise AIProviderError(f"Gemini model not found during {action} (HTTP 404)", permanent=True)
        if status == 429:
            raise AIProviderError(f"Gemini rate limit / quota exceeded during {action}")
        raise AIProviderError(f"Gemini {action} failed: {res.error or status}")

    async def list_models(self) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            res = await self._http.get(f"{API_BASE}/models", params=params, headers=self._headers(),
                                       respect_robots=False)
            self._raise_for(res, "models.list")
            data = res.json()
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" in methods:
                    ids.append(str(m.get("name", "")).removeprefix("models/"))
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    async def prepare(self) -> None:
        if self._model:
            return
        ids = await self.list_models()
        chosen = choose_model(ids, self._preference)
        if not chosen:
            raise AIProviderError("No suitable Gemini Flash model is available for this API key", permanent=True)
        self._model = chosen
        log_event(log, 20, "ai.model_resolved", provider=self.name, model=chosen)

    async def generate_json(self, system: str, prompt: str, *, max_output_tokens: int = 8192) -> Any:
        if not self._model:
            await self.prepare()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_output_tokens,
                "temperature": 0.1,
            },
        }
        res = await self._http.post(f"{API_BASE}/models/{self._model}:generateContent", json_body=body,
                                    headers=self._headers())
        self._raise_for(res, "generateContent")
        try:
            data = res.json()
        except ValueError as exc:
            raise AIProviderError("Gemini returned a non-JSON HTTP body") from exc
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise AIProviderError(f"Gemini blocked the prompt: {block}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise AIProviderError("Gemini returned no candidates")
        cand = candidates[0]
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise AIProviderError(f"Gemini returned an empty answer (finishReason={cand.get('finishReason')})")
        usage = data.get("usageMetadata") or {}
        log_event(log, 20, "ai.call", provider=self.name, model=self._model,
                  prompt_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"),
                  finish=cand.get("finishReason"))
        return extract_json(text)
