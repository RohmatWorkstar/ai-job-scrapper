"""Explainable job-to-CV matching.

score = weighted sum of four components (each 0..1), all shown to the user:
* skills      - share of the job's required (and preferred) technologies evidenced in the CV
* title       - role similarity (reuses intelligence.matching.Matcher.position)
* experience  - seniority / years fit (reuses Matcher.experience)
* keywords    - share of job keywords that literally appear in the CV text
Only CV evidence counts: a skill is "matched" when the CV text proves it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.applications.job_analyzer import JobAnalysis
from src.core.configuration import Settings
from src.core.models import JobOpportunity, MatchResult, SearchConfig
from src.cv.document import CVDocument
from src.cv.models import CandidateProfile
from src.discovery.query_generator import Preferences
from src.intelligence.matching import ASIA_WORDS, GLOBAL_REMOTE_WORDS, IMPLIED_SKILLS, Matcher

WEIGHTS = {"skills": 40.0, "title": 25.0, "experience": 20.0, "keywords": 15.0}


@dataclass
class MatchReport:
    overall_score: int = 0
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    matched_skills: list[dict[str, str]] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    missing_preferred: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    relevant_experience: list[dict[str, Any]] = field(default_factory=list)
    relevant_projects: list[str] = field(default_factory=list)
    recommended_sections: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    ai_assessment: str = ""
    method: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has(text_lower: str, term: str) -> bool:
    return bool(re.search(rf"(?<![\w+#.]){re.escape(term.lower())}(?![\w+#])", text_lower))


class CVMatcher:
    def __init__(self, settings: Settings, profile: CandidateProfile,
                 config: SearchConfig | None = None, preferences: Preferences | None = None) -> None:
        self.settings = settings
        self.profile = profile
        self.config = config
        self.prefs = preferences or Preferences()
        self.home_country = str(settings.get("candidate.home_country", "Indonesia"))
        cfg = settings.get("applications.ai.match_weights", WEIGHTS) or WEIGHTS
        total = sum(float(v) for v in cfg.values()) or 1.0
        self.weights = {k: float(cfg.get(k, 0)) * 100.0 / total for k in WEIGHTS}
        self.matcher = Matcher(settings, profile, config or SearchConfig(), self.prefs)

    def _cv_skills(self, doc: CVDocument) -> dict[str, str]:
        """lower-case skill -> how it is evidenced."""
        have: dict[str, str] = {}
        text_lower = doc.source_text.lower()
        for s in doc.skills:
            have[s.lower()] = self.profile.skill_evidence.get(s, "listed in the CV")
        for base, implied in IMPLIED_SKILLS.items():
            if base.lower() in have:
                for imp in implied:
                    have.setdefault(imp.lower(), f"implied by {base}")
        proven = {s.lower() for s in self.profile.skills}  # found by the skill detector (aliases: Golang -> Go)
        for s in list(have):
            if s not in proven and not _has(text_lower, s) and not have[s].startswith("implied"):
                have.pop(s)  # never claim a skill the CV text does not contain
        return have

    def match(self, job: JobOpportunity, analysis: JobAnalysis, doc: CVDocument) -> MatchReport:
        r = MatchReport(weights={k: round(v, 1) for k, v in self.weights.items()})
        have = self._cv_skills(doc)
        text_lower = doc.source_text.lower()
        req = analysis.required_skills
        pref = analysis.preferred_skills
        m_req = [s for s in req if s.lower() in have]
        m_pref = [s for s in pref if s.lower() in have and s not in m_req]
        r.matched_skills = [{"skill": s, "evidence": have[s.lower()][:160]} for s in m_req + m_pref]
        r.missing_skills = [s for s in req if s.lower() not in have]
        r.missing_preferred = [s for s in pref if s.lower() not in have]
        if req or pref:
            req_ratio = len(m_req) / len(req) if req else 1.0
            pref_ratio = len(m_pref) / len(pref) if pref else req_ratio
            skills = 0.8 * req_ratio + 0.2 * pref_ratio
        else:
            skills = 0.5
            r.concerns.append("The posting lists no explicit technologies; skills fit is uncertain.")
        mr = MatchResult()
        title = self.matcher.position(job, mr)
        experience = self.matcher.experience(job, mr)
        if analysis.experience_years_required and self.profile.experience_years is not None and \
                analysis.experience_years_required > self.profile.experience_years + 1:
            r.concerns.append(f"Asks for {analysis.experience_years_required}+ years; the CV shows about "
                              f"{self.profile.experience_years:g}.")
            experience = min(experience, 0.4)
        kws = [k for k in analysis.keywords if len(k) >= 3]
        r.matched_keywords = [k for k in kws if _has(text_lower, k)]
        keywords = len(r.matched_keywords) / len(kws) if kws else 0.5
        r.components = {"skills": round(skills, 2), "title": round(title, 2), "experience": round(experience, 2),
                        "keywords": round(keywords, 2)}
        r.overall_score = int(round(sum(r.components[k] * self.weights[k] for k in WEIGHTS)))

        terms = [t.lower() for t in analysis.technologies + r.matched_keywords]
        for i, e in enumerate(doc.experience):
            blob = " ".join([e.title, e.company, *e.bullets]).lower()
            hits = [t for t in dict.fromkeys(terms) if _has(blob, t)]
            if hits:
                r.relevant_experience.append({"index": i, "title": e.title, "company": e.company,
                                              "matched_terms": hits[:10], "score": len(hits)})
        r.relevant_experience.sort(key=lambda x: -x["score"])
        rel_projects: list[str] = []
        for pe in doc.project_entries:
            pblob = " ".join([pe.title, pe.company, *pe.bullets]).lower()
            if any(_has(pblob, t) for t in terms):
                label = " - ".join(v for v in (pe.title, pe.company) if v)
                if label and label not in rel_projects:
                    rel_projects.append(label)
        for p in doc.projects:
            if any(_has(p.lower(), t) for t in terms) and p not in rel_projects:
                rel_projects.append(p)
        r.relevant_projects = rel_projects
        r.recommended_sections = ["Summary", "Skills", "Experience"] + (["Projects"] if r.relevant_projects else []) + \
            ["Education"] + (["Certifications"] if doc.certifications else []) + (["Languages"] if doc.languages else [])

        # Align with search preferences & work-arrangement / location restrictions.
        job_blob = f"{job.company_name or ''} {job.job_title} {job.job_description[:3000]}".lower()
        for term in self.prefs.negative:
            if term and re.search(rf"(?<!\w){re.escape(term)}", job_blob):
                r.concerns.append(f"Matches excluded preference ('{term}').")
                r.overall_score = int(round(r.overall_score * 0.75))
        if self.config and self.config.work_arrangements and job.work_arrangement != "unknown":
            if job.work_arrangement not in set(self.config.work_arrangements):
                r.concerns.append(
                    f"Work arrangement is '{job.work_arrangement}', outside preferred {self.config.work_arrangements}."
                )

        r.explanation += mr.explanation[:1]
        r.explanation.append(f"Skills: {len(m_req)}/{len(req)} required" + (f", {len(m_pref)}/{len(pref)} preferred" if pref else "")
                             + " evidenced in the CV.")
        if r.missing_skills:
            r.explanation.append(f"Not evidenced in the CV: {', '.join(r.missing_skills[:8])}.")
        if mr.seniority_assessment:
            r.explanation.append(f"Experience: {mr.seniority_assessment}.")
        r.explanation.append(f"Keywords found in the CV: {len(r.matched_keywords)}/{len(kws)}.")
        if job.verification_status in ("UNVERIFIED", "NEEDS_REVIEW"):
            r.concerns.append(f"Posting status is {job.verification_status}: confirm it is open before applying.")
        if job.location_restrictions:
            restr_text = " ".join(job.location_restrictions)
            if not (GLOBAL_REMOTE_WORDS.search(restr_text) or ASIA_WORDS.search(restr_text)
                    or self.home_country.lower() in restr_text.lower()):
                r.concerns.append(
                    f"Location restrictions ({', '.join(job.location_restrictions[:5])}) may not include {self.home_country}."
                )
            else:
                r.concerns.append(f"Location restrictions: {', '.join(job.location_restrictions[:5])}.")
        if len(r.missing_skills) > max(2, len(req) // 2):
            r.concerns.append("More than half of the required technologies are not evidenced in the CV.")
        return r
