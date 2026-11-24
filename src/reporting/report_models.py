"""Report data model shared by the DOCX/XLSX generators and the metadata JSON."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.models import (
    ContentKind,
    ExecutedQuery,
    JobOpportunity,
    RelevanceCategory,
    SearchConfig,
    SourceRunReport,
    SourceType,
    VerificationStatus,
)
from src.cv.models import CandidateProfile
from src.utils.date_time import format_local, iso

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

STATUS_LABEL = {
    "success": "Searched - results found",
    "partial": "Searched - partial errors",
    "no_results": "Searched - no matching results",
    "failed": "Failed",
    "skipped_no_credentials": "Not searched - credentials missing",
    "not_selected": "Not searched - not selected",
    "disabled": "Not searched - disabled in settings",
    "interrupted": "Interrupted",
    "pending": "Not started",
    "running": "Running",
}
REVIEW_STATUSES = {VerificationStatus.NEEDS_REVIEW.value, VerificationStatus.CLOSED.value,
                   VerificationStatus.INACCESSIBLE.value, VerificationStatus.POSSIBLE_SCAM.value}


def safe_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    s = _CONTROL.sub("", str(value))
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def apply_channel_label(j: JobOpportunity) -> str:
    has_email = bool(j.application_emails)
    has_url = bool(j.application_url)
    if has_email and has_url:
        return "Email & Web"
    if has_email:
        return "Email"
    if has_url:
        return "Web/ATS"
    return "Manual/Unknown"


def salary_display(j: JobOpportunity) -> str:
    if j.salary_min is not None:
        rng = f"{j.salary_min:,.0f}" + (f" - {j.salary_max:,.0f}" if j.salary_max else "")
        return f"{j.salary_currency or ''} {rng}".strip() + (f" / {j.salary_period}" if j.salary_period else "") + \
            (f"  (as stated: {j.salary_text})" if j.salary_text and len(j.salary_text) < 80 else "")
    if j.salary_text:
        return f"As stated: {j.salary_text}"
    return "Not disclosed"


@dataclass
class ReportData:
    session_id: str
    started_at: datetime
    completed_at: datetime
    timezone: str
    status: str
    profile: CandidateProfile
    config: SearchConfig
    jobs: list[JobOpportunity]
    total_discoveries: int
    duplicates_merged: int
    source_reports: list[SourceRunReport]
    queries: list[ExecutedQuery]
    ai_status: str = ""
    search_providers: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    home_country: str = "Indonesia"
    new_jobs: int = 0
    countries_searched: list[str] = field(default_factory=list)
    application_statuses: dict[str, str] = field(default_factory=dict)
    applications: list[dict[str, Any]] = field(default_factory=list)

    # ---------------------------------------------------------------- formatting
    def fmt(self, dt: datetime | None, with_time: bool = True) -> str:
        return format_local(dt, self.timezone, "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")

    # ------------------------------------------------------------------ groups
    def sorted_jobs(self) -> list[JobOpportunity]:
        return sorted(self.jobs, key=lambda j: (-(j.relevance_score or 0), j.job_title.lower()))

    def by_category(self, cat: RelevanceCategory) -> list[JobOpportunity]:
        return [j for j in self.sorted_jobs() if j.relevance_category == cat.value]

    def social_and_community(self) -> list[JobOpportunity]:
        return [j for j in self.sorted_jobs() if j.source_type in (SourceType.SOCIAL_MEDIA.value, SourceType.COMMUNITY.value)
                or j.content_kind in (ContentKind.SOCIAL_POST.value, ContentKind.COMMUNITY_POST.value)
                or any(s.source_type in (SourceType.SOCIAL_MEDIA.value, SourceType.COMMUNITY.value) for s in j.additional_sources)]

    def international(self) -> dict[str, list[JobOpportunity]]:
        out: dict[str, list[JobOpportunity]] = {}
        for j in self.sorted_jobs():
            if j.relevance_category == RelevanceCategory.NOT_RELEVANT.value:
                continue
            if j.country and j.country != self.home_country:
                out.setdefault(j.country, []).append(j)
            elif not j.country and j.work_arrangement == "remote":
                out.setdefault("Remote (country not specified)", []).append(j)
        return dict(sorted(out.items(), key=lambda kv: (kv[0].startswith("Remote"), kv[0])))

    def needs_attention(self) -> list[JobOpportunity]:
        return [j for j in self.sorted_jobs() if j.verification_status in REVIEW_STATUSES
                or j.relevance_category == RelevanceCategory.NEEDS_MANUAL_REVIEW.value]

    # ------------------------------------------------------------------ counts
    def category_counts(self) -> dict[str, int]:
        c = Counter(j.relevance_category for j in self.jobs)
        return {cat.value: c.get(cat.value, 0) for cat in RelevanceCategory}

    def verification_counts(self) -> dict[str, int]:
        c = Counter(j.verification_status for j in self.jobs)
        return {s.value: c.get(s.value, 0) for s in VerificationStatus}

    def country_counts(self) -> dict[str, int]:
        return dict(Counter(j.country or "Unknown / not stated" for j in self.jobs).most_common())

    def source_type_counts(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for j in self.jobs:
            c[j.source_type] += 1
            for s in j.additional_sources:
                c[s.source_type] += 1
        return {SourceType(k).label if k in SourceType._value2member_map_ else k: v for k, v in c.most_common()}

    def platform_counts(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for j in self.jobs:
            c[re.sub(r"\s*\(.*\)$", "", j.source_name)] += 1
            for s in j.additional_sources:
                c[re.sub(r"\s*\(.*\)$", "", s.source_name)] += 1
        return dict(c.most_common())

    def sources_by_status(self) -> dict[str, list[SourceRunReport]]:
        out: dict[str, list[SourceRunReport]] = {}
        for r in self.source_reports:
            out.setdefault(r.status, []).append(r)
        return out

    def summary_counts(self) -> dict[str, int]:
        cats = self.category_counts()
        ver = self.verification_counts()
        return {
            "total_discoveries": self.total_discoveries,
            "unique_opportunities": len(self.jobs),
            "duplicates_merged": self.duplicates_merged,
            "highly_relevant": cats[RelevanceCategory.HIGHLY_RELEVANT.value],
            "relevant": cats[RelevanceCategory.RELEVANT.value],
            "potentially_relevant": cats[RelevanceCategory.POTENTIALLY_RELEVANT.value],
            "needs_manual_review": cats[RelevanceCategory.NEEDS_MANUAL_REVIEW.value],
            "not_relevant": cats[RelevanceCategory.NOT_RELEVANT.value],
            "verified_open": ver[VerificationStatus.VERIFIED_OPEN.value],
            "likely_open": ver[VerificationStatus.LIKELY_OPEN.value],
            "unverified": ver[VerificationStatus.UNVERIFIED.value],
            "needs_review": ver[VerificationStatus.NEEDS_REVIEW.value],
            "closed": ver[VerificationStatus.CLOSED.value],
            "inaccessible": ver[VerificationStatus.INACCESSIBLE.value],
            "possible_scam": ver[VerificationStatus.POSSIBLE_SCAM.value],
            "new_since_previous_searches": self.new_jobs,
            "email_applicable": sum(1 for j in self.jobs if j.application_emails),
            "queries_executed": len(self.queries),
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "application": "AI Job Intelligence & Discovery System",
            "session_id": self.session_id,
            "status": self.status,
            "timezone": self.timezone,
            "search_started_at": iso(self.started_at),
            "search_started_at_local": self.fmt(self.started_at),
            "search_completed_at": iso(self.completed_at),
            "search_completed_at_local": self.fmt(self.completed_at),
            "candidate_profile": self.profile.to_public_json()["candidate_profile"],
            "search_configuration": self.config.to_dict(),
            "countries_searched": self.countries_searched,
            "search_providers": self.search_providers,
            "ai": self.ai_status,
            "counts": self.summary_counts(),
            "relevance_categories": self.category_counts(),
            "verification_summary": self.verification_counts(),
            "sources": [r.to_dict() for r in self.source_reports],
            "queries": [q.to_dict() for q in self.queries],
            "limitations": self.limitations,
            "errors": self.errors,
        }
