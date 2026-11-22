"""Search history: new-vs-seen detection, previously discovered jobs, saved preferences."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from src.core.models import JobOpportunity
from src.storage.repository import Repository
from src.utils.date_time import from_iso, iso, utc_now


class SearchHistory:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def mark_new(self, jobs: list[JobOpportunity]) -> int:
        """Flag jobs never seen in earlier sessions. Old jobs are NOT suppressed."""
        known = self.repo.known_job_keys([j.job_id for j in jobs])
        new = 0
        for j in jobs:
            info = known.get(j.job_id)
            if info:
                j.is_new = False
                j.first_seen_at = from_iso(info.get("first_seen_at"))
                prev = info.get("last_verification_status")
                if prev and prev != j.verification_status:
                    j.signals["previous_verification_status"] = prev
            else:
                j.is_new = True
                j.first_seen_at = j.discovered_at
                new += 1
        return new

    def previous_jobs(self, session_id: str, days: int = 30) -> list[JobOpportunity]:
        since = iso(utc_now() - timedelta(days=days)) or ""
        jobs = self.repo.previous_opportunities(session_id, since)
        for j in jobs:
            j.from_history = True
            j.is_new = False
            msg = "Found in an earlier search, not rediscovered in this one; status re-checked where possible."
            if msg not in j.warnings:
                j.warnings.append(msg)
        return jobs


class PreferenceStore:
    """Reusable questionnaire answers (JSON file, no personal CV data)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
