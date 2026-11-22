"""Pipeline stage + live counter state, rendered by the CLI. Only real, measured values."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

STAGES = [
    "CV analysis",
    "Search query generation",
    "Multi-source discovery",
    "Information extraction",
    "Verification",
    "Job matching",
    "Report generation",
]

WAITING, RUNNING, COMPLETED, FAILED, SKIPPED, INTERRUPTED = (
    "WAITING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED", "INTERRUPTED")


@dataclass
class PipelineState:
    stages: dict[str, str] = field(default_factory=lambda: {s: WAITING for s in STAGES})
    details: dict[str, str] = field(default_factory=dict)
    queries_by_category: Counter = field(default_factory=Counter)
    sources_running: set[str] = field(default_factory=set)
    sources_done: dict[str, str] = field(default_factory=dict)
    results_discovered: int = 0
    unique_opportunities: int | None = None
    pages_fetched: int = 0
    pages_to_fetch: int = 0
    ai_calls: int = 0
    messages: list[str] = field(default_factory=list)
    listener: Callable[["PipelineState"], None] | None = None

    def _notify(self) -> None:
        if self.listener:
            try:
                self.listener(self)
            except Exception:  # noqa: BLE001 - display problems must not stop the pipeline
                pass

    def set(self, stage: str, status: str, detail: str = "") -> None:
        self.stages[stage] = status
        if detail:
            self.details[stage] = detail
        self._notify()

    def message(self, text: str) -> None:
        self.messages.append(text)
        self.messages[:] = self.messages[-6:]
        self._notify()

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "query":
            self.queries_by_category[data.get("category", "other")] += 1
        elif kind == "source_start":
            self.sources_running.add(data["source"])
        elif kind == "source_done":
            self.sources_running.discard(data["source"])
            self.sources_done[data["source"]] = data.get("status", "")
        self._notify()

    def add_results(self, n: int) -> None:
        self.results_discovered += n
        self._notify()
