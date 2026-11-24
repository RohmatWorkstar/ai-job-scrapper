"""Configurable eligibility and relevance filters (``applications.eligibility`` in settings.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.core.configuration import Settings
from src.core.models import JobOpportunity
from src.extraction.content_parser import SENIORITY_ORDER
from src.utils.date_time import age_days


@dataclass
class EligibilityResult:
    eligible: bool
    reasons: list[str] = field(default_factory=list)     # why it was rejected
    passed: list[str] = field(default_factory=list)      # checks that passed (shown in review)


def _kw_match(text: str, keywords: list[str]) -> str | None:
    for k in keywords:
        if k and re.search(rf"(?i)(?<!\w){re.escape(k.strip())}(?!\w)", text):
            return k
    return None


class EligibilityFilter:
    def __init__(self, settings: Settings, overrides: dict[str, Any] | None = None,
                 candidate_seniority: str | None = None) -> None:
        cfg = dict(settings.get("applications.eligibility", {}) or {})
        cfg.update(overrides or {})
        self.min_score = float(cfg.get("min_relevance_score", 58))
        self.categories = [c.upper() for c in cfg.get("allowed_relevance_categories", ["HIGHLY RELEVANT", "RELEVANT"]) or []]
        self.excluded_status = set(cfg.get("excluded_verification_statuses",
                                           ["CLOSED", "POSSIBLE_SCAM", "INACCESSIBLE"]) or [])
        self.include_kw = list(cfg.get("title_include_keywords", []) or [])
        self.exclude_kw = list(cfg.get("title_exclude_keywords", []) or [])
        self.exclude_companies = [c.lower() for c in cfg.get("exclude_companies", []) or []]
        self.employment_types = list(cfg.get("allowed_employment_types", []) or [])
        self.max_age_days = cfg.get("max_posting_age_days", 45)
        self.require_email = bool(cfg.get("require_email", True))
        self.onsite_abroad = bool(cfg.get("allow_onsite_abroad", False))
        self.home_country = str(settings.get("candidate.home_country", "Indonesia") or "")
        gap = cfg.get("max_seniority_gap", 1)
        self.max_seniority_gap = None if gap is None else int(gap)
        self.candidate_seniority = candidate_seniority

    def evaluate(self, job: JobOpportunity) -> EligibilityResult:
        r = EligibilityResult(True)

        def fail(msg: str) -> None:
            r.eligible = False
            r.reasons.append(msg)

        if job.verification_status in self.excluded_status:
            fail(f"Verification status {job.verification_status} is excluded.")
        score = job.relevance_score
        if score is None or score < self.min_score:
            fail(f"Relevance score {score if score is not None else 'n/a'} is below {self.min_score:g}.")
        else:
            r.passed.append(f"Relevance score {score:g} >= {self.min_score:g}")
        if self.categories and (job.relevance_category or "").upper() not in self.categories:
            fail(f"Relevance category '{job.relevance_category}' is not in {self.categories}.")
        title = job.job_title or ""
        if self.include_kw and not _kw_match(title, self.include_kw):
            fail(f"Title does not contain any of {self.include_kw}.")
        hit = _kw_match(title, self.exclude_kw)
        if hit:
            fail(f"Title contains excluded keyword '{hit}'.")
        if job.company_name and job.company_name.lower() in self.exclude_companies:
            fail(f"Company '{job.company_name}' is excluded.")
        if self.employment_types and job.employment_type not in ("unknown", *self.employment_types):
            fail(f"Employment type '{job.employment_type}' is not in {self.employment_types}.")
        if self.max_age_days:
            age = age_days(job.published_at or job.post_created_at)
            if age is not None and age > float(self.max_age_days):
                fail(f"Posting is {age:.0f} days old (max {self.max_age_days}).")
        if self.require_email:
            if not job.application_emails:
                fail("No application e-mail address in the posting.")
        else:
            if not (job.application_url or job.source_url):
                fail("No application URL or source URL in the posting.")
        if not self.onsite_abroad and self.home_country and job.work_arrangement in ("onsite", "hybrid")                 and job.country and job.country.lower() != self.home_country.lower():
            fail(f"{job.work_arrangement.capitalize()} role in {job.country} (outside {self.home_country}); "
                 "set allow_onsite_abroad: true to include it.")
        cand, lvl = self.candidate_seniority, job.seniority
        if self.max_seniority_gap is not None and cand in SENIORITY_ORDER and lvl in SENIORITY_ORDER:
            gap = SENIORITY_ORDER.index(lvl) - SENIORITY_ORDER.index(cand)
            if gap > self.max_seniority_gap:
                fail(f"Role level '{lvl}' is {gap} levels above your estimated level '{cand}' "
                     f"(max_seniority_gap={self.max_seniority_gap}).")
        return r
