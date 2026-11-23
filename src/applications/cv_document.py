"""Structured view of the original CV (the single source of truth for generated CVs).

Everything here is copied from the CV text. The optional AI pass only helps to *segment*
messy CV text into entries; each AI-returned line must be found (near-)verbatim in the CV,
otherwise it is discarded.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.ai.prompts import cv_structure_prompt, redact_personal
from src.ai.provider_manager import AIManager
from src.applications.models import CandidateIdentity
from src.cv.analyzer import _RANGE_RE, ROLE_WORDS, Sections, split_sections
from src.cv.models import CandidateProfile, ParsedCV
from src.extraction.contact_parser import EMAIL_RE

_BULLET = re.compile(r"^\s*[•\-\*–·▪●◦»]\s*")
_PHONE = re.compile(r"(?<![\w/])\+?\d[\d\s().-]{7,}\d(?![\w/])")
_LINK = re.compile(r"(?i)\b(?:https?://\S+|(?:www\.)?(?:linkedin\.com|github\.com|gitlab\.com)/\S+)")
_SPLIT_TITLE = re.compile(r"\s+(?:-|–|—|at|@|\|)\s+|,\s+")
_LIST_SPLIT = re.compile(r"\s*[,;|•·]\s*|\n")


@dataclass
class ExperienceEntry:
    title: str = ""
    company: str = ""
    dates: str = ""
    location: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class CVDocument:
    identity: CandidateIdentity = field(default_factory=CandidateIdentity)
    headline: str = ""
    summary: str = ""
    experience: list[ExperienceEntry] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    method: str = "deterministic"
    notes: list[str] = field(default_factory=list)
    source_text: str = ""
    source_sha256: str = ""

    def to_dict(self, include_source: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_source:
            d.pop("source_text", None)
        return d

    def ai_safe_dict(self) -> dict[str, Any]:
        """Content the AI may see: no name, e-mail, phone, links or location."""
        return {
            "headline": self.headline, "summary": redact_personal(self.summary),
            "experience": [{"id": f"e{i}", "title": e.title, "company": e.company, "dates": e.dates,
                            "bullets": [{"id": f"e{i}b{j}", "text": redact_personal(b)} for j, b in enumerate(e.bullets)]}
                           for i, e in enumerate(self.experience)],
            "skills": self.skills, "projects": [redact_personal(p) for p in self.projects],
            "education": self.education, "certifications": self.certifications, "languages": self.languages,
        }


# ------------------------------------------------------------------ identity
def extract_identity(text: str, profile: CandidateProfile | None = None) -> CandidateIdentity:
    header = [l.strip() for l in text.splitlines()[:12] if l.strip()]
    ident = CandidateIdentity()
    for line in header[:3]:
        words = line.split()
        if 2 <= len(words) <= 5 and not re.search(r"[\d@|/:]", line) and not ROLE_WORDS.search(line):
            ident.name = line.strip()
            break
    joined = "\n".join(header)
    m = EMAIL_RE.search(joined)
    ident.email = m.group(0) if m else ""
    for pm in _PHONE.finditer(joined):
        if not re.fullmatch(r"\d{4}\s*[-–]\s*\d{4}", pm.group(0).strip()):
            ident.phone = pm.group(0).strip()
            break
    ident.links = list(dict.fromkeys(l.group(0).rstrip(".,;") for l in _LINK.finditer(joined)))[:3]
    ident.location = (profile.location if profile else "") or ""
    for line in header[1:4]:
        for seg in re.split(r"\s*\|\s*", line):
            if profile and profile.location and profile.location.lower() in seg.lower() and len(seg) < 60:
                ident.location = seg.strip()
    # Explicit overrides (optional) from .env: APPLICANT_NAME, APPLICANT_EMAIL, APPLICANT_PHONE, ...
    ident.name = os.environ.get("APPLICANT_NAME", "").strip() or ident.name
    ident.email = os.environ.get("APPLICANT_EMAIL", "").strip() or ident.email
    ident.phone = os.environ.get("APPLICANT_PHONE", "").strip() or ident.phone
    ident.location = os.environ.get("APPLICANT_LOCATION", "").strip() or ident.location
    links = [l.strip() for l in os.environ.get("APPLICANT_LINKS", "").split(",") if l.strip()]
    ident.links = links or ident.links
    return ident


# ---------------------------------------------------------------- structure
def _split_title_company(line: str) -> tuple[str, str]:
    parts = [p.strip(" ,-|") for p in _SPLIT_TITLE.split(line, maxsplit=1) if p.strip(" ,-|")]
    if len(parts) == 2:
        a, b = parts
        # "Company - Title" ordering: the part with role words is the title.
        if ROLE_WORDS.search(b) and not ROLE_WORDS.search(a):
            return b, a
        return a, b
    return line.strip(), ""


def parse_experience(section: str) -> list[ExperienceEntry]:
    entries: list[ExperienceEntry] = []
    cur: ExperienceEntry | None = None
    lines = [l.rstrip() for l in section.splitlines()]
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        is_bullet = bool(_BULLET.match(line))
        text = _BULLET.sub("", line).strip()
        rng = _RANGE_RE.search(text)
        rest = _RANGE_RE.sub("", text).strip(" ,|-–()") if rng else text
        if not is_bullet and rng and len(rest) < 4 and cur is not None:
            cur.dates = rng.group(0).strip()
            continue
        next_line = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
        header_like = (not is_bullet and len(text) <= 120 and not text.endswith(".") and
                       (ROLE_WORDS.search(rest) is not None or (bool(_RANGE_RE.search(next_line)) and
                                                               len(_RANGE_RE.sub("", next_line).strip()) < 4)))
        if header_like:
            title, company = _split_title_company(rest)
            cur = ExperienceEntry(title=title, company=company, dates=rng.group(0).strip() if rng else "")
            entries.append(cur)
            continue
        if cur is None:
            cur = ExperienceEntry()
            entries.append(cur)
        if not is_bullet and cur.bullets and text[:1].islower():
            cur.bullets[-1] = f"{cur.bullets[-1]} {text}"
        else:
            cur.bullets.append(text)
    return [e for e in entries if e.title or e.bullets]


def _items(section: str, limit: int, max_len: int = 200) -> list[str]:
    out: list[str] = []
    for line in section.splitlines():
        l = _BULLET.sub("", line).strip()
        if 3 <= len(l) <= max_len and l not in out:
            out.append(l)
    return out[:limit]


def structure_cv(cv: ParsedCV, profile: CandidateProfile) -> CVDocument:
    sections: Sections = split_sections(cv.text)
    by = sections.by_name
    skills = list(profile.skills)
    lower = {s.lower() for s in skills}
    for item in _LIST_SPLIT.split(by.get("skills", "")):
        item = _BULLET.sub("", item).strip(" .")
        if 2 <= len(item) <= 40 and item.lower() not in lower:
            skills.append(item)
            lower.add(item.lower())
    doc = CVDocument(
        identity=extract_identity(cv.text, profile),
        headline=profile.current_title,
        summary=" ".join(l.strip() for l in by.get("summary", "").splitlines() if l.strip()),
        experience=parse_experience(by.get("experience", "")),
        skills=skills,
        education=_items(by.get("education", ""), 6) or list(profile.education),
        projects=_items(by.get("projects", ""), 8) or list(profile.projects),
        certifications=_items(by.get("certifications", ""), 10) or list(profile.certifications),
        languages=_items(by.get("languages", ""), 5) or list(profile.languages),
        source_text=cv.text,
        source_sha256=cv.sha256,
    )
    if not doc.experience:
        doc.notes.append("No work-experience entries could be segmented from the CV text.")
    return doc


def _verbatim_norm(text: str) -> str:
    text = re.sub(r"(?m)^\s*[•\-\*–·▪●◦»]\s*", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _verbatim(value: str, cv_norm: str) -> bool:
    v = _verbatim_norm(value)
    return len(v) >= 2 and v in cv_norm


async def structure_cv_ai(cv: ParsedCV, profile: CandidateProfile, ai: AIManager | None) -> CVDocument:
    """Deterministic structure, replaced by an AI segmentation only when every line is verified."""
    doc = structure_cv(cv, profile)
    if ai is None or not ai.available:
        return doc
    result = await ai.ask_json("cv_structure", cv_structure_prompt(redact_personal(cv.text[:30000])), cache_days=30)
    if not isinstance(result, dict):
        return doc
    # Segmentation may only copy: every value must occur verbatim (case/whitespace/bullet-insensitive)
    # in the CV. Fuzzy token overlap is not enough ("led" would match inside "skilled").
    cv_norm = _verbatim_norm(cv.text)
    entries: list[ExperienceEntry] = []
    dropped = 0
    for e in result.get("experience") or []:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()[:120]
        company = str(e.get("company") or "").strip()[:120]
        if not title or not _verbatim(title, cv_norm) or (company and not _verbatim(company, cv_norm)):
            dropped += 1
            continue
        bullets = []
        for b in e.get("bullets") or []:
            b = _BULLET.sub("", str(b)).strip()
            if b and _verbatim(b, cv_norm):
                bullets.append(b[:400])
            else:
                dropped += 1
        dates = str(e.get("dates") or "").strip()[:40]
        entries.append(ExperienceEntry(title=title, company=company,
                                       dates=dates if dates and _verbatim(dates, cv_norm) else "",
                                       location="", bullets=bullets))
    total_bullets = sum(len(e.bullets) for e in entries)
    if entries and total_bullets >= sum(len(e.bullets) for e in doc.experience):
        doc.experience = entries
        doc.method = "deterministic+ai_segmentation"
    if dropped:
        doc.notes.append(f"{dropped} AI-segmented item(s) discarded: not found verbatim in the CV.")
    return doc
