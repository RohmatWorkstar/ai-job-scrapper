"""AI provider interface. New providers (OpenAI, Anthropic, ...) implement this class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIProvider(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def model(self) -> str:
        """Resolved model identifier (may be empty before :meth:`prepare`)."""

    @abstractmethod
    async def prepare(self) -> None:
        """Resolve the model and validate credentials. Raises AIProviderError."""

    @abstractmethod
    async def generate_json(self, system: str, prompt: str, *, max_output_tokens: int = 8192) -> Any:
        """Return parsed JSON produced by the model. Raises AIProviderError."""

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None
