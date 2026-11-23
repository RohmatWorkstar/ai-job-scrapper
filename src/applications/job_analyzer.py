"""Job analysis: requirements, responsibilities, technologies and keywords of one posting.

Deterministic extraction always runs. An AI pass can add structure, but every AI item
must be supported by the posting text (token overlap) or it is dropped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.ai.prompts import job_analysis_prompt
from src.ai.provider_manager import AIManager
from src.core.configuration import Settings
from src.core.models import JobOpportunity
from src.cv.profile_builder import token_overlap
from src.extraction.content_parser import SkillDetector, detect_years_required

_RESP_HEAD = re.compile(r"(?i)^(responsibilities|what you('| wi)ll do|your role|the role|job description|duties|"
                        r"tanggung jawab|deskripsi pekerjaan|key responsibilities)\b")
_REQ_HEAD = re.compile(r"(?i)^(requirements|qualifications|what (we're|we are) looking for|who you are|must have|"
                       r"you have|skills|kualifikasi|persyaratan|syarat)\b")
_OTHER_HEAD = re.compile(r"(?i)^(benefits|perks|about (us|the company)|why join|what we offer|salary|how to apply|"
                         r"cara melamar|keuntungan)\b")
_BULLET = re.compile(r"^\s*[•\-\*–·▪●◦]\s*")
_STOP = set("the and for with you your our are will have has from that this into able work team job role "
            "years year experience strong good using within about also must should".split())


@dataclass
class JobAnalysis:
    job_title: str
    company: str
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    experience_years_required: int | None = None
    education_requirement: str | None = None
    keywords: list[str] = field(default_factory=list)
    language: str = "en"
    method: str = "deterministic"
    notes: list[str] = field(default_factory=list)

    @property
    def technologies(self) -> list[str]:
        return list(dict.fromkeys(self.required_skills + self.preferred_skills))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["technologies"] = self.technologies
        return d


def _sectioned_lines(description: str) -> tuple[list[str], list[str]]:
    resp: list[str] = []
    req: list[str] = []
    bucket: list[str] | None = None
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.rstrip(":").strip()
        if len(head) <= 60 and _RESP_HEAD.match(head):
            bucket = resp
            continue
        if len(head) <= 60 and _REQ_HEAD.match(head):
            bucket = req
            continue
        if len(head) <= 60 and _OTHER_HEAD.match(head):
            bucket = None
            continue
        if bucket is not None and 8 <= len(line) <= 300:
            bucket.append(_BULLET.sub("", line))
    return resp[:15], req[:15]


def detect_language(text: str) -> str:
    words = re.findall(r"[a-z]+", text.lower()[:3000])
    id_words = sum(1 for w in words if w in {"dan", "yang", "dengan", "untuk", "kami", "anda", "pengalaman", "lowongan",
                                             "minimal", "kirim", "lamaran", "kerja", "memiliki", "mampu"})
    return "id" if words and id_words / len(words) > 0.04 else "en"


def keywords_for(job: JobOpportunity, skills: list[str]) -> list[str]:
    words = [w.strip(".") for w in re.findall(r"[a-z][a-z0-9+#.]{2,}", job.job_title.lower()) if w not in _STOP]
    out: dict[str, str] = {}
    for k in skills + words:
        out.setdefault(k.lower(), k)
    return list(out.values())[:25]


class JobAnalyzer:
    def __init__(self, settings: Settings, ai: AIManager | None = None) -> None:
        self.settings = settings
        self.ai = ai
        self.detector = SkillDetector(settings.taxonomy)

    def analyze_deterministic(self, job: JobOpportunity) -> JobAnalysis:
        desc = job.job_description or ""
        resp, req = _sectioned_lines(desc)
        a = JobAnalysis(job_title=job.job_title, company=job.company_name or "",
                        required_skills=list(job.required_skills), preferred_skills=list(job.preferred_skills),
                        responsibilities=resp, requirements=req, experience_years_required=detect_years_required(desc),
                        language=detect_language(f"{job.job_title}\n{desc}"))
        if not a.required_skills and not a.preferred_skills:
            a.required_skills = self.detector.detect_names(desc)
        a.keywords = keywords_for(job, a.technologies)
        if not desc:
            a.notes.append("The posting has no description text; analysis is based on the title only.")
        return a

    async def analyze(self, job: JobOpportunity) -> JobAnalysis:
        a = self.analyze_deterministic(job)
        if self.ai is None or not self.ai.available or len(job.job_description or "") < 200:
            return a
        result = await self.ai.ask_json("job_analysis", job_analysis_prompt(job.job_title, job.company_name or "",
                                                                            job.job_description), cache_days=14)
        if not isinstance(result, dict):
            a.notes.append("AI job analysis unavailable; deterministic analysis used.")
            return a
        text_lower = re.sub(r"\s+", " ", f"{job.job_title}\n{job.job_description}".lower())
        dropped = 0

        def supported_list(key: str, threshold: float, limit: int) -> list[str]:
            nonlocal dropped
            out = []
            for v in result.get(key) or []:
                if isinstance(v, str) and v.strip() and token_overlap(text_lower, v) >= threshold:
                    out.append(v.strip()[:300])
                elif v:
                    dropped += 1
            return out[:limit]

        for key, attr in (("required_skills", "required_skills"), ("preferred_skills", "preferred_skills")):
            for s in supported_list(key, 0.99, 25):
                canon = self.detector.canonical(s) or s
                if canon not in a.required_skills and canon not in a.preferred_skills:
                    getattr(a, attr).append(canon)
        a.responsibilities = supported_list("responsibilities", 0.6, 15) or a.responsibilities
        a.requirements = supported_list("requirements", 0.6, 15) or a.requirements
        a.keywords = list(dict.fromkeys(a.keywords + supported_list("keywords", 0.99, 15)))[:30]
        years = result.get("experience_years_required")
        if a.experience_years_required is None and isinstance(years, (int, float)) and 0 < years < 30 and \
                re.search(rf"\b{int(years)}\b", text_lower):
            a.experience_years_required = int(years)
        edu = result.get("education_requirement")
        if isinstance(edu, str) and token_overlap(text_lower, edu) >= 0.6:
            a.education_requirement = edu[:200]
        a.method = "deterministic+ai"
        if dropped:
            a.notes.append(f"{dropped} AI item(s) dropped: not supported by the posting text.")
        return a
