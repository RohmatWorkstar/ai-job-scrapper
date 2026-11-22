"""Transparent relevance scoring: deterministic components + optional AI role assessment.

The score is an *estimated relevance indicator* (0-100, integer), not a probability of
being hired. Each component is 0..1; missing information yields a neutral value and
an explicit "needs verification" note rather than a penalty-to-zero.
"""

from __future__ import annotations

import re
from typing import Any

from src.ai.prompts import matching_prompt
from src.ai.provider_manager import AIManager
from src.core.configuration import Settings
from src.core.models import JobOpportunity, MatchResult, RelevanceCategory, SearchConfig
from src.cv.models import CandidateProfile
from src.discovery.query_generator import Preferences
from src.extraction.content_parser import SENIORITY_ORDER, detect_years_required
from src.intelligence.deduplication import norm_title
from src.utils.date_time import age_days

DEFAULT_WEIGHTS = {"position": 25, "skills": 25, "experience": 15, "location": 15, "salary": 10, "freshness": 10}
GLOBAL_REMOTE_WORDS = re.compile(r"(?i)\b(worldwide|anywhere|global|any\s+location|international|all\s+countries)\b")
ASIA_WORDS = re.compile(r"(?i)\b(asia|apac|asia[\s-]pacific|sea|south[\s-]?east\s+asia|gmt\+7|utc\+7|emea\s*&\s*apac)\b")
NON_TECH_WORDS = re.compile(r"(?i)\b(sales|account executive|recruiter|marketing|legal|accountant|nurse|driver|warehouse)\b")
GENERIC_SWE = re.compile(r"(?i)\b(software|engineer|developer|programmer)\b")
IMPLIED_SKILLS = {
    "PostgreSQL": ["SQL"], "MySQL": ["SQL"], "MariaDB": ["SQL"], "Microsoft SQL Server": ["SQL"],
    "Oracle Database": ["SQL"], "SQLite": ["SQL"], "TypeScript": ["JavaScript"], "Node.js": ["JavaScript"],
    "React": ["JavaScript"], "Next.js": ["React", "JavaScript"], "Express": ["Node.js", "JavaScript"],
    "NestJS": ["Node.js", "TypeScript"], "Laravel": ["PHP"], "CodeIgniter": ["PHP"], "Symfony": ["PHP"],
    "Django": ["Python"], "Flask": ["Python"], "FastAPI": ["Python"], "Spring Boot": ["Java"],
    "Ruby on Rails": ["Ruby"], "Flutter": ["Dart"],
}


class Matcher:
    def __init__(self, settings: Settings, profile: CandidateProfile, config: SearchConfig,
                 preferences: Preferences | None = None) -> None:
        self.settings = settings
        self.profile = profile
        self.config = config
        self.prefs = preferences or Preferences()
        w = settings.get("matching.weights", DEFAULT_WEIGHTS) or DEFAULT_WEIGHTS
        total = sum(float(v) for v in w.values()) or 1.0
        self.weights = {k: float(w.get(k, 0)) * 100.0 / total for k in DEFAULT_WEIGHTS}
        th = settings.get("matching.thresholds", {}) or {}
        self.t_high = float(th.get("highly_relevant", 72))
        self.t_rel = float(th.get("relevant", 58))
        self.t_pot = float(th.get("potentially_relevant", 40))
        self.t_review = float(th.get("needs_review_min", 25))
        fams = settings.profiles.get("role_families") or {}
        self.direct_kw: list[str] = []
        self.adjacent_kw: list[str] = []
        direct_fams = set(profile.role_families[:3]) if profile.role_families else set()
        for fam in profile.role_families:
            if fam == "software_engineering":
                continue
            kws = [k.lower() for k in fams.get(fam, {}).get("keywords", [])]
            (self.direct_kw if fam in direct_fams else self.adjacent_kw).extend(kws)
        self.targets = [norm_title(t) for t in (config.target_positions or profile.target_positions)]
        self.adjacent_targets = [norm_title(t) for t in profile.adjacent_positions]
        self.cand_skills = {s.lower() for s in profile.skills}
        # Matching-only inference (the profile itself is never changed): a proven skill implies
        # its underlying language/category, e.g. PostgreSQL -> SQL.
        self.implied: dict[str, str] = {}
        for have, implied in IMPLIED_SKILLS.items():
            if have.lower() in self.cand_skills:
                for s in implied:
                    if s.lower() not in self.cand_skills:
                        self.implied.setdefault(s.lower(), have)
        self.cand_skills |= set(self.implied)
        self.home = settings.get("candidate.home_country", "Indonesia")
        self.cand_level = profile.seniority_estimate if profile.seniority_estimate in SENIORITY_ORDER else None

    # ------------------------------------------------------------ components
    def position(self, job: JobOpportunity, r: MatchResult) -> float:
        title = norm_title(job.job_title)
        if not title or title == "(untitled)":
            r.needs_verification.append("Job title unclear")
            return 0.3
        best = 0.0
        stripped = re.sub(r"\b(senior|sr|junior|jr|lead|principal|staff|mid|intern|head|ii|iii|i)\b", "", title).strip()
        for t in self.targets:
            ts = set(t.split()) - {"senior", "junior", "lead"}
            js = set(stripped.split())
            if ts and js:
                best = max(best, len(ts & js) / len(ts | js) * 1.1)
            if t and t in title:
                best = max(best, 1.0)
        if any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", title) for k in self.direct_kw):
            best = max(best, 0.95)
        elif any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", title) for k in self.adjacent_kw):
            best = max(best, 0.75)
        for t in self.adjacent_targets:
            if t and t in title:
                best = max(best, 0.75)
        if best < 0.5 and GENERIC_SWE.search(title) and not NON_TECH_WORDS.search(title):
            best = max(best, 0.6 if "software" in title else 0.45)
        if NON_TECH_WORDS.search(title):
            best = min(best, 0.15)
        best = min(1.0, best)
        if best >= 0.9:
            r.explanation.append(f"Title '{job.job_title}' matches the target roles.")
        elif best >= 0.6:
            r.explanation.append(f"Title '{job.job_title}' is related to the candidate's roles.")
        else:
            r.explanation.append(f"Title '{job.job_title}' is only loosely related to the target roles.")
        return best

    def skills(self, job: JobOpportunity, r: MatchResult) -> float:
        req = job.required_skills
        pref = job.preferred_skills
        if not req and not pref:
            r.needs_verification.append("Technical requirements not stated in the available text")
            return 0.5
        m_req = [s for s in req if s.lower() in self.cand_skills]
        m_pref = [s for s in pref if s.lower() in self.cand_skills]
        r.matched_skills = m_req + [s for s in m_pref if s not in m_req]
        r.missing_skills = [s for s in req if s.lower() not in self.cand_skills][:12]
        req_ratio = len(m_req) / len(req) if req else (len(m_pref) / len(pref))
        pref_ratio = len(m_pref) / len(pref) if pref else req_ratio
        score = 0.8 * req_ratio + 0.2 * pref_ratio
        if len(m_req) >= 4:
            score = max(score, 0.75)  # many concrete overlaps outweigh long wish-lists
        if r.matched_skills:
            shown = [f"{s} (implied by {self.implied[s.lower()]})" if s.lower() in self.implied else s
                     for s in r.matched_skills[:10]]
            r.explanation.append(f"Matching skills: {', '.join(shown)}.")
        if r.missing_skills:
            r.explanation.append(f"Requirements not evidenced in the CV: {', '.join(r.missing_skills[:8])}.")
        return min(1.0, score)

    def experience(self, job: JobOpportunity, r: MatchResult) -> float:
        score = 0.6
        level = job.seniority if job.seniority in SENIORITY_ORDER else None
        if self.config.seniority_levels and self.config.seniority_mode == "custom" and level and \
                level not in self.config.seniority_levels:
            r.seniority_assessment = f"Job level '{level}' is outside the selected levels."
            return 0.2
        if level and self.cand_level:
            diff = SENIORITY_ORDER.index(level) - SENIORITY_ORDER.index(self.cand_level)
            score = {0: 1.0, 1: 0.6, -1: 0.7, 2: 0.2, -2: 0.4}.get(diff, 0.1 if diff > 0 else 0.3)
            r.seniority_assessment = (
                "Seniority appropriate" if diff == 0 else
                f"Stretch: role is {level}, candidate estimated {self.cand_level}" if diff > 0 else
                f"Role ({level}) is below the candidate's estimated level ({self.cand_level})")
        else:
            r.seniority_assessment = "Seniority not stated" if not level else "Candidate seniority unknown"
            r.needs_verification.append("Seniority level")
        years = detect_years_required(job.job_description)
        if years is not None and self.profile.experience_years is not None:
            if years > self.profile.experience_years + 1:
                score = min(score, 0.4)
                r.seniority_assessment += f"; asks for {years}+ years (CV shows ~{self.profile.experience_years})"
            elif years <= self.profile.experience_years:
                score = max(score, 0.8 if score >= 0.6 else score)
                r.seniority_assessment += f"; experience requirement ({years}+ years) met"
        return score

    def location(self, job: JobOpportunity, r: MatchResult) -> float:
        prefs = set(self.config.work_arrangements or ["remote", "hybrid", "onsite"])
        arr = job.work_arrangement
        targeted = set(self.config.countries)
        worldwide = set(self.settings.get("search.worldwide_countries", []) or [])
        if arr == "remote":
            if "remote" not in prefs:
                r.arrangement_assessment = "Remote role, but remote work was not selected"
                return 0.3
            restr = " ".join(job.location_restrictions + [job.location or ""])
            if job.location_restrictions or (job.location and job.location.strip()):
                if GLOBAL_REMOTE_WORDS.search(restr) or (self.home and self.home.lower() in restr.lower()) or ASIA_WORDS.search(restr):
                    r.arrangement_assessment = "Remote, open to the candidate's region"
                    return 1.0
                if job.location_restrictions:
                    r.arrangement_assessment = f"Remote but restricted to: {', '.join(job.location_restrictions)[:80]}"
                    r.needs_verification.append("Remote eligibility for candidates based in " + str(self.home))
                    return 0.35
                if job.country and job.country != self.home:
                    # e.g. "Remote, United Kingdom": usually remote *within* that country
                    r.arrangement_assessment = f"Remote within {job.country} (eligibility from {self.home} unclear)"
                    r.needs_verification.append("Remote eligibility for candidates based in " + str(self.home))
                    return 0.4
            r.arrangement_assessment = "Remote (eligible locations not stated)"
            r.needs_verification.append("Remote eligibility by country")
            return 0.75
        country = job.country
        if arr in ("hybrid", "onsite"):
            if arr not in prefs:
                r.arrangement_assessment = f"{arr.title()} role, but {arr} work was not selected"
                return 0.25
            if country and country in targeted:
                r.arrangement_assessment = f"{arr.title()} in {country} (selected country)"
                return 1.0
            if country and self.config.include_worldwide:
                r.arrangement_assessment = f"{arr.title()} in {country}: relocation / work permit likely required"
                return 0.55 if country in worldwide else 0.35
            if country:
                r.arrangement_assessment = f"{arr.title()} in {country}, outside the selected countries"
                return 0.1
            r.arrangement_assessment = f"{arr.title()}; location country unclear"
            r.needs_verification.append("Job location")
            return 0.5
        # unknown arrangement
        r.needs_verification.append("Work arrangement")
        if country and country in targeted:
            r.arrangement_assessment = f"Arrangement not stated; located in {country}"
            return 0.75
        if country:
            ok = self.config.include_worldwide
            r.arrangement_assessment = f"Arrangement not stated; located in {country}"
            return 0.45 if ok else 0.15
        r.arrangement_assessment = "Arrangement and location not stated"
        return 0.5

    def salary(self, job: JobOpportunity, r: MatchResult) -> float:
        if job.salary_min is None and not job.salary_text:
            r.salary_assessment = "Salary not disclosed"
            return 0.6 if self.config.include_undisclosed_salary else 0.2
        if job.salary_min is None:
            r.salary_assessment = f"Salary stated as text: {job.salary_text[:60]}"
            return 0.7
        shown = f"{job.salary_currency or ''} {job.salary_min:,.0f}" + (f"-{job.salary_max:,.0f}" if job.salary_max else "") + \
                (f" per {job.salary_period}" if job.salary_period else "")
        if not self.config.min_salary:
            r.salary_assessment = f"Salary disclosed: {shown.strip()}"
            return 0.85
        if self.config.salary_currency and job.salary_currency and job.salary_currency != self.config.salary_currency:
            r.salary_assessment = f"Salary {shown.strip()} is in a different currency (not converted)"
            r.needs_verification.append("Salary comparison across currencies")
            return 0.6
        top = job.salary_max or job.salary_min
        if top >= self.config.min_salary:
            r.salary_assessment = f"Salary {shown.strip()} meets the minimum"
            return 1.0
        r.salary_assessment = f"Salary {shown.strip()} is below the minimum ({self.config.min_salary:,.0f})"
        return 0.2

    def freshness(self, job: JobOpportunity, r: MatchResult) -> float:
        dt = job.published_at or job.post_created_at
        a = age_days(dt)
        if a is None:
            r.needs_verification.append("Publication date")
            return 0.4
        if a > 60:
            job.signals["stale"] = True
        return 1.0 if a <= 3 else 0.9 if a <= 7 else 0.75 if a <= 14 else 0.55 if a <= 30 else 0.3

    # ------------------------------------------------------------------ main
    def score(self, job: JobOpportunity) -> MatchResult:
        r = MatchResult()
        comps = {
            "position": self.position(job, r),
            "skills": self.skills(job, r),
            "experience": self.experience(job, r),
            "location": self.location(job, r),
            "salary": self.salary(job, r),
            "freshness": self.freshness(job, r),
        }
        total = sum(self.weights[k] * v for k, v in comps.items())
        types = set(self.config.employment_types)
        if types and "all" not in types and job.employment_type != "unknown" and job.employment_type not in types:
            total *= 0.7
            r.explanation.append(f"Employment type '{job.employment_type}' is not among the selected types.")
        blob = f"{job.company_name or ''} {job.job_title} {job.job_description[:3000]}".lower()
        for term in self.prefs.negative:
            if term and re.search(rf"(?<!\w){re.escape(term)}", blob):
                total *= 0.6
                r.explanation.append(f"Matches an excluded preference ('{term}').")
        for term in self.prefs.positive:
            if term and re.search(rf"(?<!\w){re.escape(term)}", blob):
                total = min(100.0, total + 5)
                r.explanation.append(f"Matches a stated preference ('{term}').")
        r.components = {k: round(v, 2) for k, v in comps.items()}
        r.score = float(round(total))
        r.needs_verification = list(dict.fromkeys(r.needs_verification))
        return r

    def categorize(self, job: JobOpportunity) -> str:
        s = job.match.score
        if job.signals.get("not_a_job"):
            return RelevanceCategory.NOT_RELEVANT.value
        unknowns = len(job.match.needs_verification)
        comp = job.match.components
        pos, loc, exp, sk = (comp.get("position", 0), comp.get("location", 0), comp.get("experience", 0),
                             comp.get("skills", 0))
        # Gates: a weighted average must not hide a decisive mismatch (wrong role, ineligible
        # location, far-off seniority) behind strong scores elsewhere.
        if s >= self.t_high and unknowns <= 3 and pos >= 0.75 and loc >= 0.7 and exp >= 0.5 and sk >= 0.5:
            return RelevanceCategory.HIGHLY_RELEVANT.value
        if s >= self.t_rel and pos >= 0.5 and loc >= 0.3:
            return RelevanceCategory.RELEVANT.value
        if s >= self.t_rel:
            return RelevanceCategory.POTENTIALLY_RELEVANT.value
        if s >= self.t_pot:
            return RelevanceCategory.POTENTIALLY_RELEVANT.value
        if s >= self.t_review and unknowns >= 3:
            return RelevanceCategory.NEEDS_MANUAL_REVIEW.value
        return RelevanceCategory.NOT_RELEVANT.value

    def apply(self, job: JobOpportunity) -> None:
        job.match = self.score(job)
        job.relevance_score = job.match.score
        job.relevance_category = self.categorize(job)

    def recompute_total(self, job: JobOpportunity) -> None:
        comps = job.match.components
        base = sum(self.weights[k] * comps.get(k, 0) for k in self.weights)
        # keep preference / employment adjustments proportional
        old_base = sum(self.weights[k] * v for k, v in job.match.components.get("_orig", {}).items()) or None
        factor = (job.match.score / old_base) if old_base else 1.0
        job.match.score = float(round(min(100.0, base * factor)))
        job.relevance_score = job.match.score
        job.relevance_category = self.categorize(job)


async def ai_rerank(jobs: list[JobOpportunity], matcher: Matcher, ai: AIManager, profile: CandidateProfile,
                    config: SearchConfig, settings: Settings) -> int:
    """Blend an AI role-equivalence judgement into the position/experience components."""
    if not ai.available:
        return 0
    limit = int(settings.get("ai.max_jobs_for_ai_matching", 60))
    batch_size = int(settings.get("ai.matching_batch_size", 10))
    pool = sorted((j for j in jobs if (j.match.score or 0) >= matcher.t_review and not j.signals.get("not_a_job")),
                  key=lambda j: -(j.match.score or 0))[:limit]
    candidate = profile.search_safe_summary()
    prefs: dict[str, Any] = {"work_arrangements": config.work_arrangements, "countries": config.countries,
                             "employment_types": config.employment_types,
                             "additional": config.additional_preferences[:300]}
    done = 0
    for start in range(0, len(pool), batch_size):
        batch = pool[start:start + batch_size]
        payload = [{"id": str(i), "title": j.job_title, "company": j.company_name, "location": j.location,
                    "arrangement": j.work_arrangement, "seniority": j.seniority, "skills": j.required_skills[:15],
                    "description": j.job_description[:1200]} for i, j in enumerate(batch)]
        result = await ai.ask_json("job_matching", matching_prompt(candidate, prefs, payload), cache_days=3)
        if not isinstance(result, dict):
            if not ai.available or ai.remaining <= 0:
                break
            continue
        for item in result.get("items", []) or []:
            try:
                job = batch[int(item.get("id"))]
                eq = float(item.get("role_equivalence"))
            except (TypeError, ValueError, IndexError):
                continue
            eq = max(0.0, min(1.0, eq))
            comps = dict(job.match.components)
            orig = {k: v for k, v in comps.items() if not k.startswith("_")}
            comps["_orig"] = orig
            comps["position"] = round(0.5 * orig.get("position", 0) + 0.5 * eq, 2)
            fit = str(item.get("seniority_fit") or "unknown")
            if job.seniority == "unknown" and fit in {"appropriate", "stretch", "below", "overqualified"}:
                comps["experience"] = {"appropriate": 0.9, "stretch": 0.55, "below": 0.65, "overqualified": 0.6}[fit]
            job.match.components = comps
            explanation = str(item.get("explanation") or "")[:300]
            if explanation:
                job.match.ai_assessment = explanation
            for c in (item.get("concerns") or [])[:4]:
                if isinstance(c, str) and c.strip():
                    job.match.explanation.append(f"AI note - check: {c.strip()[:120]}")
            matcher.recompute_total(job)
            job.match.components = {k: v for k, v in comps.items() if not k.startswith("_")}
            done += 1
    return done
