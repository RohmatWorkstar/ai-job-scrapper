"""Shared data models: search configuration, discoveries, opportunities, evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Any

from src.utils.date_time import from_iso, iso


class SourceType(str, Enum):
    SEARCH_ENGINE = "search_engine"
    JOB_PORTAL = "job_portal"
    COMPANY_CAREERS = "company_careers"
    ATS = "ats_platform"
    SOCIAL_MEDIA = "social_media"
    COMMUNITY = "community"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "search_engine": "Search Engines",
            "job_portal": "Job Portals",
            "company_careers": "Official Company Career Pages",
            "ats_platform": "ATS Platforms",
            "social_media": "Public Social Media Posts",
            "community": "Public Online Communities",
            "other": "Other Public Sources",
        }[self.value]


class VerificationStatus(str, Enum):
    VERIFIED_OPEN = "VERIFIED_OPEN"
    LIKELY_OPEN = "LIKELY_OPEN"
    UNVERIFIED = "UNVERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CLOSED = "CLOSED"
    INACCESSIBLE = "INACCESSIBLE"
    POSSIBLE_SCAM = "POSSIBLE_SCAM"


class RelevanceCategory(str, Enum):
    HIGHLY_RELEVANT = "HIGHLY RELEVANT"
    RELEVANT = "RELEVANT"
    POTENTIALLY_RELEVANT = "POTENTIALLY RELEVANT"
    NEEDS_MANUAL_REVIEW = "NEEDS MANUAL REVIEW"
    NOT_RELEVANT = "NOT RELEVANT"


class ContentKind(str, Enum):
    JOB_POSTING = "job_posting"          # structured listing (ATS/API/JSON-LD)
    SOCIAL_POST = "social_post"          # recruiter/HR/company post
    COMMUNITY_POST = "community_post"    # forum/community thread
    SEARCH_RESULT = "search_result"      # indexed page known only via search snippet
    NOT_A_JOB = "not_a_job"


@dataclass
class Evidence:
    field: str
    value: str
    excerpt: str
    source_url: str
    retrieved_at: str
    method: str  # api_field | json_ld | html_text | search_snippet | ai_interpretation | http_check | cv_text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRef:
    source_type: str
    source_name: str
    source_url: str
    discovered_at: str
    published_at: str | None = None
    title: str = ""
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RawDiscovery:
    """What a source connector returns before normalisation."""

    source_type: SourceType
    source_name: str
    source_url: str
    title: str
    retrieved_at: datetime
    snippet: str = ""
    text: str = ""
    company: str | None = None
    company_website: str | None = None
    location: str | None = None
    country: str | None = None
    application_url: str | None = None
    published_at: datetime | None = None
    post_created_at: datetime | None = None
    published_at_evidence: str = ""
    employment_type: str | None = None
    work_arrangement: str | None = None
    seniority: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_text: str | None = None
    tags: list[str] = field(default_factory=list)
    external_id: str | None = None
    official_channel: bool = False          # company-owned channel (ATS board / career site)
    listed_in_live_feed: bool = False       # present in a live API listing at retrieval time
    structured: bool = False                # came from an API / structured data
    content_kind: ContentKind = ContentKind.JOB_POSTING
    query: str = ""
    language: str | None = None
    location_restrictions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, datetime):
                v = iso(v)
            elif isinstance(v, Enum):
                v = v.value
            d[f.name] = v
        d.pop("raw", None)
        return d


@dataclass
class MatchResult:
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    seniority_assessment: str = ""
    arrangement_assessment: str = ""
    salary_assessment: str = ""
    needs_verification: list[str] = field(default_factory=list)
    ai_assessment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobOpportunity:
    job_id: str
    job_title: str
    company_name: str | None = None
    company_website: str | None = None
    country: str | None = None
    city: str | None = None
    location: str | None = None
    work_arrangement: str = "unknown"
    employment_type: str = "unknown"
    seniority: str = "unknown"
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_text: str | None = None
    job_description: str = ""
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    post_created_at: datetime | None = None
    discovered_at: datetime | None = None
    source_type: str = SourceType.OTHER.value
    source_name: str = ""
    source_url: str = ""
    application_url: str | None = None
    verification_status: str = VerificationStatus.UNVERIFIED.value
    verification_reason: str = ""
    relevance_score: float | None = None
    relevance_category: str = RelevanceCategory.NEEDS_MANUAL_REVIEW.value
    match: MatchResult = field(default_factory=MatchResult)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    additional_sources: list[SourceRef] = field(default_factory=list)
    possible_duplicates: list[str] = field(default_factory=list)
    content_kind: str = ContentKind.JOB_POSTING.value
    official_channel: bool = False
    listed_in_live_feed: bool = False
    external_id: str | None = None
    location_restrictions: list[str] = field(default_factory=list)
    query: str = ""
    is_new: bool = True
    first_seen_at: datetime | None = None
    from_history: bool = False
    page_fetched: bool = False
    # Machine signals used by verification (fetch result, AI classification, job-signal score).
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def all_source_urls(self) -> list[str]:
        urls = [self.source_url] + [s.source_url for s in self.additional_sources]
        return [u for i, u in enumerate(urls) if u and u not in urls[:i]]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, datetime):
                v = iso(v)
            elif isinstance(v, MatchResult):
                v = v.to_dict()
            elif f.name == "evidence":
                v = [e.to_dict() for e in v]
            elif f.name == "additional_sources":
                v = [s.to_dict() for s in v]
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobOpportunity":
        kwargs = dict(d)
        for k in ("published_at", "post_created_at", "discovered_at", "first_seen_at"):
            kwargs[k] = from_iso(kwargs.get(k))
        kwargs["match"] = MatchResult(**(kwargs.get("match") or {}))
        kwargs["evidence"] = [Evidence(**e) for e in kwargs.get("evidence") or []]
        kwargs["additional_sources"] = [SourceRef(**s) for s in kwargs.get("additional_sources") or []]
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in kwargs.items() if k in allowed})


@dataclass
class SearchConfig:
    geography_mode: str = "indonesia_worldwide"
    countries: list[str] = field(default_factory=lambda: ["Indonesia"])
    include_worldwide: bool = True
    employment_types: list[str] = field(default_factory=lambda: ["full_time"])
    position_mode: str = "ai_recommended"
    target_positions: list[str] = field(default_factory=list)
    work_arrangements: list[str] = field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    publication_window_days: int | None = 7
    min_salary: float | None = None
    salary_currency: str | None = None
    include_undisclosed_salary: bool = True
    seniority_mode: str = "ai_recommended"
    seniority_levels: list[str] = field(default_factory=list)
    source_categories: list[str] = field(default_factory=lambda: [s.value for s in SourceType])
    languages: list[str] = field(default_factory=lambda: ["en", "id"])
    additional_preferences: str = ""
    include_previous_jobs: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SearchConfig":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed})

    @property
    def wants_indonesia(self) -> bool:
        return "Indonesia" in self.countries

    def all_countries(self, worldwide_list: list[str]) -> list[str]:
        out = list(self.countries)
        if self.include_worldwide:
            out += [c for c in worldwide_list if c not in out]
        return out


@dataclass
class QuerySpec:
    text: str
    category: SourceType
    purpose: str            # e.g. "portal:JobStreet", "social:LinkedIn Posts", "general"
    language: str = "en"
    country: str | None = None
    country_code: str | None = None
    site: str | None = None
    role: str | None = None
    freshness_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


@dataclass
class SourceRunReport:
    name: str
    category: str
    status: str = "pending"   # success | partial | failed | no_results | skipped_no_credentials | not_selected | disabled | interrupted
    queries_executed: int = 0
    items_scanned: int = 0
    results: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutedQuery:
    source_name: str
    category: str
    query: str
    status: str
    result_count: int
    executed_at: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
