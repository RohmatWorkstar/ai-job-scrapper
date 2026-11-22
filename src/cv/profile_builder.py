"""Build the final candidate profile: deterministic analysis + validated AI interpretation.

Anti-fabrication rules applied to AI output:
* A skill is "proven" only if it (or its evidence quote) appears in the CV text.
  Otherwise it is moved to ``transferable_skills`` (clearly labelled as suggested).
* Certifications / education / projects must share most of their words with the CV.
* Seniority never increases beyond the deterministic estimate.
* "Senior"/"Lead" prefixes are removed from suggested titles unless the CV supports them.
"""

from __future__ import annotations

import re
from typing import Any

from src.ai.prompts import cv_analysis_prompt, redact_personal
from src.ai.provider_manager import AIManager
from src.core.configuration import Settings
from src.cv.analyzer import CVAnalyzer, seniority_from_years
from src.cv.models import CandidateProfile, ParsedCV
from src.extraction.content_parser import SENIORITY_ORDER, SkillDetector

_CAT_MAP = {
    "programming_language": "programming_languages", "framework": "frameworks", "database": "databases",
    "tool": "tools", "other": "tools",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def appears_in(text_lower: str, value: str) -> bool:
    v = _norm(value)
    return bool(v) and v in text_lower


def token_overlap(text_lower: str, value: str) -> float:
    tokens = [t for t in re.findall(r"[a-z0-9+#.]{3,}", value.lower())]
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t in text_lower) / len(tokens)


def _lower_seniority(a: str, b: str) -> str:
    if a not in SENIORITY_ORDER:
        return b if b in SENIORITY_ORDER else ""
    if b not in SENIORITY_ORDER:
        return a
    return a if SENIORITY_ORDER.index(a) <= SENIORITY_ORDER.index(b) else b


def _strip_seniority_prefix(title: str) -> str:
    return re.sub(r"(?i)^(senior|sr\.?|lead|principal|staff|head of)\s+", "", title).strip()


def merge_ai_profile(base: CandidateProfile, ai: dict[str, Any], cv_text: str, detector: SkillDetector) -> CandidateProfile:
    text_lower = _norm(cv_text)
    p = CandidateProfile.from_dict(base.to_dict())
    p.analysis_method = "deterministic+ai"

    # ---- skills
    proven = {s.lower() for s in p.skills}
    for item in ai.get("skills") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"]).strip()[:60]
        canonical = detector.canonical(name) or name
        quote = str(item.get("evidence") or "")
        supported = appears_in(text_lower, name) or (quote and appears_in(text_lower, quote[:80]) and
                                                     (appears_in(quote.lower(), name) or detector.canonical(name)))
        if canonical.lower() in proven:
            continue
        if supported:
            cat = detector.category_of(canonical) or _CAT_MAP.get(str(item.get("category", "other")), "tools")
            getattr(p, cat).append(canonical)
            p.skills.append(canonical)
            proven.add(canonical.lower())
            if quote:
                p.skill_evidence[canonical] = quote[:160]
        elif canonical.lower() not in {t.lower() for t in p.transferable_skills}:
            p.transferable_skills.append(canonical)
    for item in ai.get("transferable_skills") or []:
        name = str(item.get("name") if isinstance(item, dict) else item).strip()[:60]
        if name and name.lower() not in proven and name.lower() not in {t.lower() for t in p.transferable_skills}:
            p.transferable_skills.append(name)

    # ---- soft skills / industries / experience areas (must be evidenced)
    p.soft_skills = [s for s in (ai.get("soft_skills") or []) if isinstance(s, str) and appears_in(text_lower, s)][:10]
    for ind in ai.get("industry_experience") or []:
        if isinstance(ind, str) and token_overlap(text_lower, ind) >= 0.6 and ind.lower() not in {i.lower() for i in p.industry_experience}:
            p.industry_experience.append(ind)
    for area in ai.get("experience_areas") or []:
        if isinstance(area, str) and area not in p.experience_areas:
            p.experience_areas.append(area)

    # ---- factual lists validated against CV wording
    def validated(key: str, threshold: float, limit: int) -> list[str]:
        out = []
        for v in ai.get(key) or []:
            if isinstance(v, str) and v.strip() and token_overlap(text_lower, v) >= threshold:
                out.append(v.strip()[:200])
        return out[:limit]

    for key, thr, lim in (("certifications", 0.6, 10), ("education", 0.5, 5), ("projects", 0.5, 8)):
        vals = validated(key, thr, lim)
        if vals:
            setattr(p, key, vals)
    rejected_certs = [c for c in ai.get("certifications") or [] if isinstance(c, str) and token_overlap(text_lower, c) < 0.6]
    if rejected_certs:
        p.notes.append(f"{len(rejected_certs)} AI-suggested certification(s) ignored: not found in the CV text.")
    langs = [l for l in ai.get("languages") or [] if isinstance(l, str) and token_overlap(text_lower, l) >= 0.5]
    if langs:
        p.languages = list(dict.fromkeys(p.languages + langs))

    # ---- title / experience / seniority
    ai_title = str(ai.get("current_title") or "").strip()
    if ai_title and appears_in(text_lower, ai_title) and (not p.current_title or len(ai_title) < len(p.current_title) + 25):
        p.current_title = ai_title
    if p.experience_years is None and isinstance(ai.get("experience_years"), (int, float)) and 0 < ai["experience_years"] < 50:
        p.experience_years = float(ai["experience_years"])
        p.notes.append("Experience years estimated by AI from the CV (no explicit dates parsed deterministically).")
    det_sen = p.seniority_estimate or seniority_from_years(p.experience_years)
    ai_sen = str(ai.get("seniority_estimate") or "").lower()
    p.seniority_estimate = _lower_seniority(det_sen, ai_sen) if det_sen else (ai_sen if ai_sen in SENIORITY_ORDER else "")

    # ---- positions
    allow_senior = p.seniority_estimate in {"senior", "lead"}
    direct, adjacent = list(p.target_positions), list(p.adjacent_positions)
    for item in ai.get("target_positions") or []:
        title = str(item.get("title") if isinstance(item, dict) else item).strip()[:80]
        if not title:
            continue
        if not allow_senior:
            title = _strip_seniority_prefix(title)
        bucket = adjacent if isinstance(item, dict) and item.get("directness") == "adjacent" else direct
        if title.lower() not in {t.lower() for t in direct + adjacent}:
            bucket.append(title)
    p.target_positions = direct[:10]
    p.adjacent_positions = adjacent[:8]

    loc = str(ai.get("location") or "").strip()
    if not p.location and loc and token_overlap(text_lower, loc) >= 0.5:
        p.location = loc
    summary = str(ai.get("summary") or "").strip()
    if summary:
        p.summary = summary[:500]
    return p


class ProfileBuilder:
    def __init__(self, settings: Settings, ai: AIManager | None) -> None:
        self.settings = settings
        self.ai = ai
        self.analyzer = CVAnalyzer(settings)

    async def build(self, cv: ParsedCV, *, use_ai: bool) -> CandidateProfile:
        profile = self.analyzer.analyze(cv)
        if not use_ai or self.ai is None or not self.ai.available:
            if self.ai is not None and not self.ai.available:
                profile.notes.append(f"AI analysis not used: {self.ai.disabled_reason}.")
            return profile
        prompt = cv_analysis_prompt(redact_personal(cv.text[:30000]), profile.skills)
        result = await self.ai.ask_json("cv_analysis", prompt, cache_days=30)
        if not isinstance(result, dict):
            profile.notes.append("AI analysis unavailable for this run; deterministic profile used.")
            return profile
        return merge_ai_profile(profile, result, cv.text, self.analyzer.skills)
