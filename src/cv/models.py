"""Candidate profile model. Every field is derived from the CV text."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class ParsedCV:
    filename: str
    file_format: str
    text: str
    pages: int
    sha256: str
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class CandidateProfile:
    current_title: str = ""
    target_positions: list[str] = field(default_factory=list)
    experience_years: float | None = None
    skills: list[str] = field(default_factory=list)                 # all proven technical skills
    programming_languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    soft_skills: list[str] = field(default_factory=list)
    industry_experience: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    seniority_estimate: str = ""
    geographical_preferences: list[str] = field(default_factory=list)
    # --- transparency fields ---
    experience_areas: list[str] = field(default_factory=list)       # e.g. "Backend Development"
    role_families: list[str] = field(default_factory=list)
    adjacent_positions: list[str] = field(default_factory=list)     # plausible but less direct roles
    transferable_skills: list[str] = field(default_factory=list)    # suggested, NOT proven by the CV
    skill_evidence: dict[str, str] = field(default_factory=dict)    # skill -> CV excerpt
    location: str = ""
    summary: str = ""
    analysis_method: str = "deterministic"
    notes: list[str] = field(default_factory=list)
    cv_filename: str = ""
    cv_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_json(self) -> dict[str, Any]:
        """Spec-shaped JSON (``candidate_profile``) without evidence excerpts."""
        keys = [
            "current_title", "target_positions", "experience_years", "skills", "programming_languages",
            "frameworks", "databases", "tools", "industry_experience", "projects", "education",
            "certifications", "languages", "seniority_estimate", "geographical_preferences",
        ]
        return {"candidate_profile": {k: getattr(self, k) for k in keys}}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CandidateProfile":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed})

    def search_safe_summary(self) -> dict[str, Any]:
        """Non-identifying subset that may be sent to AI providers for matching."""
        return {
            "current_title": self.current_title,
            "target_positions": self.target_positions,
            "experience_years": self.experience_years,
            "seniority_estimate": self.seniority_estimate,
            "skills": self.skills,
            "industry_experience": self.industry_experience,
            "languages": self.languages,
        }
