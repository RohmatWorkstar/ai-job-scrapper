"""Evidence helpers: building evidence records and human-readable evidence summaries."""

from __future__ import annotations

from src.core.models import Evidence, JobOpportunity
from src.utils.date_time import iso, utc_now


def add_evidence(job: JobOpportunity, field: str, value: str, excerpt: str, method: str, url: str | None = None) -> None:
    for e in job.evidence:
        if e.field == field and e.value == value and e.method == method:
            return
    job.evidence.append(Evidence(field=field, value=str(value)[:200], excerpt=str(excerpt)[:300],
                                 source_url=url or job.source_url, retrieved_at=iso(utc_now()) or "", method=method))


def evidence_lines(job: JobOpportunity, limit: int = 8) -> list[str]:
    """Compact human-readable lines for reports: 'field: value [method] - "excerpt"'."""
    lines = []
    for e in job.evidence[:limit]:
        excerpt = f' - "{e.excerpt[:120]}"' if e.excerpt and e.excerpt != e.value else ""
        lines.append(f"{e.field}: {e.value[:100]} [{e.method}]{excerpt}")
    return lines


def evidence_by_field(job: JobOpportunity) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for e in job.evidence:
        out.setdefault(e.field, []).append(e)
    return out
