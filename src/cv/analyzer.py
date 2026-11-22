"""Deterministic CV analysis. Extracts only what is literally present in the CV text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.core.configuration import Settings
from src.cv.models import CandidateProfile, ParsedCV
from src.extraction.content_parser import CountryDetector, SkillDetector, detect_seniority

SECTION_KEYWORDS: dict[str, list[str]] = {
    "summary": ["summary", "profile", "about me", "professional summary", "career objective", "objective",
                "ringkasan", "tentang saya", "profil"],
    "experience": ["experience", "work experience", "professional experience", "employment history", "work history",
                   "employment", "career history", "pengalaman kerja", "pengalaman", "riwayat pekerjaan"],
    "education": ["education", "pendidikan", "academic background", "riwayat pendidikan", "academic"],
    "skills": ["skills", "technical skills", "keahlian", "kemampuan", "tech stack", "technologies", "competencies",
               "core competencies", "skills & tools", "hard skills", "skill"],
    "projects": ["projects", "personal projects", "proyek", "portfolio", "selected projects", "key projects", "project"],
    "certifications": ["certifications", "certificates", "certification", "sertifikasi", "sertifikat",
                       "licenses & certifications", "licenses and certifications", "licences & certifications"],
    "languages": ["languages", "language", "bahasa", "language skills"],
    "other": ["references", "hobbies", "interests", "awards", "achievements", "organizations", "organisasi",
              "volunteer", "volunteering", "publications", "courses", "training", "pelatihan"],
}

ROLE_WORDS = re.compile(
    r"(?i)\b(developer|engineer|programmer|analyst|manager|designer|consultant|architect|lead|specialist|intern|"
    r"scientist|administrator|tester|devops|sre|officer|staff|technician|coordinator|head|director|pengembang|"
    r"full[\s-]?stack|back[\s-]?end|front[\s-]?end)\b"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "agu": 8, "agt": 8,
    "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "des": 12,
}
_MONTH_RE = r"(?:jan|feb|mar|apr|may|mei|jun|jul|aug|agu|agt|sep|oct|okt|nov|dec|des)[a-z]*\.?"


def _date_token(p: str) -> str:
    return (
        rf"(?:(?P<{p}m>{_MONTH_RE})\s+(?P<{p}y>(?:19|20)\d{{2}})"
        rf"|(?P<{p}nm>\d{{1,2}})\s*/\s*(?P<{p}ny>(?:19|20)\d{{2}})"
        rf"|(?P<{p}oy>(?:19|20)\d{{2}}))"
    )


_RANGE_RE = re.compile(
    _date_token("a")
    + r"\s*(?:-|–|—|to|until|till|sampai|s/d|s\.d\.)\s*"
    + r"(?:(?P<present>present|current|now|today|sekarang|saat\s+ini|ongoing)|"
    + _date_token("b")
    + r")",
    re.IGNORECASE,
)
_EXPLICIT_YEARS_RE = re.compile(
    r"(?i)(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?|tahun)\s+(?:of\s+)?(?:professional\s+|working\s+|industry\s+|hands-on\s+)?(?:experience|pengalaman)"
)
_PROFICIENCY_RE = re.compile(r"(?i)\b(fluent|native|proficient|intermediate|advanced|basic|conversational|toeic|ielts|toefl|mother\s+tongue|aktif|pasif|bilingual)\b")

FAMILY_AREAS = {
    "fullstack": "Fullstack Engineering", "backend": "Backend Development", "frontend": "Frontend Development",
    "node": "Node.js Development", "php": "PHP / Laravel Development", "python": "Python Development",
    "java": "Java Development", "mobile": "Mobile Development", "devops": "DevOps / Cloud Infrastructure",
    "data_engineering": "Data Engineering", "data_science": "Data Analysis / Science", "ai_ml": "AI / Machine Learning",
    "qa": "Quality Assurance / Testing", "software_engineering": "Software Development",
}


@dataclass
class Sections:
    by_name: dict[str, str] = field(default_factory=dict)
    header_lines: list[str] = field(default_factory=list)


def _header_name(line: str) -> str | None:
    s = re.sub(r"[^a-zA-Z&/ ]", "", line).strip().lower()
    if not s or len(s) > 40 or len(line.strip()) > 45:
        return None
    for name, words in SECTION_KEYWORDS.items():
        for w in words:
            if s == w or (s.startswith(w) and len(s) <= len(w) + 12) or s.endswith(" " + w):
                return name
    return None


def split_sections(text: str) -> Sections:
    lines = text.splitlines()
    out = Sections(header_lines=[l.strip() for l in lines[:12] if l.strip()])
    current = "header"
    buf: dict[str, list[str]] = {"header": []}
    for line in lines:
        name = _header_name(line)
        if name:
            current = name
            buf.setdefault(current, [])
            continue
        buf.setdefault(current, []).append(line)
    out.by_name = {k: "\n".join(v).strip() for k, v in buf.items() if "\n".join(v).strip()}
    return out


def _month_index(token: str | None) -> int:
    if not token:
        return 1
    return _MONTHS.get(token.lower()[:3], 1)


def _token_date(m: re.Match[str], p: str, end: bool) -> date | None:
    if m.group(f"{p}y"):
        return date(int(m.group(f"{p}y")), _month_index(m.group(f"{p}m")), 1)
    if m.group(f"{p}ny"):
        month = int(m.group(f"{p}nm"))
        return date(int(m.group(f"{p}ny")), month if 1 <= month <= 12 else 1, 1)
    if m.group(f"{p}oy"):
        return date(int(m.group(f"{p}oy")), 12 if end else 1, 1)
    return None


def employment_intervals(text: str, today: date | None = None) -> list[tuple[date, date]]:
    today = today or date.today()
    intervals: list[tuple[date, date]] = []
    for m in _RANGE_RE.finditer(text):
        start = _token_date(m, "a", end=False)
        end = today if m.group("present") else _token_date(m, "b", end=True)
        if not start or not end or end < start or start > today:
            continue
        end = min(end, today)
        if (end - start).days > 365 * 45:
            continue
        intervals.append((start, end))
    return intervals


def total_years(intervals: list[tuple[date, date]]) -> float | None:
    if not intervals:
        return None
    merged: list[list[date]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    days = sum((e - s).days + 30 for s, e in merged)
    return round(days / 365.25, 1)


def seniority_from_years(years: float | None) -> str:
    if years is None:
        return ""
    if years < 1:
        return "entry"
    if years < 3:
        return "junior"
    if years < 8:
        return "mid"  # conservative: without a "Senior" title, do not overstate seniority
    return "senior"  # "lead" is only assigned from explicit title evidence


def _short_lines(section: str, limit: int, max_len: int = 140) -> list[str]:
    out: list[str] = []
    for line in section.splitlines():
        l = line.strip(" •-*–·\t")
        if 3 <= len(l) <= max_len and l not in out:
            out.append(l)
        if len(out) >= limit:
            break
    return out


class CVAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skills = SkillDetector(settings.taxonomy)
        self.countries = CountryDetector(settings.country_table())
        self.families: dict[str, Any] = settings.profiles.get("role_families") or {}

    # ------------------------------------------------------------------ title
    def detect_current_title(self, sections: Sections) -> str:
        candidates = sections.header_lines[1:8]
        for line in candidates:
            if "@" in line or re.search(r"\d{6,}", line) or len(line) > 90:
                continue
            for seg in re.split(r"\s*[|•·]\s*", line):
                if ROLE_WORDS.search(seg) and len(seg) <= 60:
                    return seg.strip(" ,-")
        exp = sections.by_name.get("experience", "")
        for line in exp.splitlines()[:25]:
            l = line.strip(" •-*–·")
            if ROLE_WORDS.search(l) and len(l) <= 80 and not _RANGE_RE.search(l):
                return re.split(r"\s+(?:at|@|-|–|\|)\s+", l)[0].strip(" ,-")
            if ROLE_WORDS.search(l) and len(l) <= 120:
                cleaned = _RANGE_RE.sub("", l).strip(" ,-|–")
                return re.split(r"\s+(?:at|@|\|)\s+", cleaned)[0].strip(" ,-")
        return ""

    # --------------------------------------------------------------- families
    def role_families(self, title: str, skills: list[str]) -> list[tuple[str, float, bool]]:
        """Return (family, score, title_match) sorted by score."""
        lower_skills = {s.lower() for s in skills}
        out: list[tuple[str, float, bool]] = []
        for fam, spec in self.families.items():
            kws = [k.lower() for k in spec.get("keywords", [])]
            title_match = any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", title.lower()) for k in kws) if title else False
            signals = sum(1 for s in spec.get("signal_skills", []) if s.lower() in lower_skills)
            score = (3.0 if title_match else 0.0) + signals
            if fam == "software_engineering":
                continue
            if title_match or signals >= 2:
                out.append((fam, score, title_match))
        out.sort(key=lambda x: -x[1])
        if out or (title and ROLE_WORDS.search(title)):
            out.append(("software_engineering", 1.0, bool(title)))
        return out

    # ------------------------------------------------------------------- main
    def analyze(self, cv: ParsedCV) -> CandidateProfile:
        text = cv.text
        sections = split_sections(text)
        hits = self.skills.detect(text)
        cats: dict[str, list[str]] = {"programming_languages": [], "frameworks": [], "databases": [], "tools": []}
        evidence: dict[str, str] = {}
        for h in hits:
            cats.setdefault(h.category, []).append(h.name)
            evidence[h.name] = h.excerpt
        skills = [h.name for h in hits]

        title = self.detect_current_title(sections)
        exp_text = sections.by_name.get("experience") or "\n".join(
            v for k, v in sections.by_name.items() if k not in {"education", "certifications"}
        )
        years = total_years(employment_intervals(exp_text))
        notes: list[str] = []
        explicit = [float(m.group(1)) for m in _EXPLICIT_YEARS_RE.finditer(text)]
        if years is None and explicit:
            years = max(explicit)
            notes.append("Experience years taken from an explicit statement in the CV.")
        elif years is not None:
            notes.append("Experience years computed from employment date ranges in the CV (overlaps merged).")
        else:
            notes.append("Experience duration could not be determined from the CV.")

        title_seniority = detect_seniority(title) if title else "unknown"
        seniority = title_seniority if title_seniority != "unknown" else seniority_from_years(years)

        fams = self.role_families(title, skills)
        targets: list[str] = []
        adjacent: list[str] = []
        if title:
            targets.append(re.sub(r"(?i)^(senior|sr\.?|junior|jr\.?|lead)\s+", "", title).strip() or title)
        for fam, score, title_match in fams:
            titles = self.families.get(fam, {}).get("titles", [])
            bucket = targets if (title_match or score >= 4) else adjacent
            for t in titles[:2]:
                if t.lower() not in {x.lower() for x in targets + adjacent}:
                    bucket.append(t)
        areas = [FAMILY_AREAS.get(f, f) for f, _, _ in fams]

        industry_text = "\n".join(sections.by_name.get(k, "") for k in ("experience", "summary", "projects", "header"))
        industries = [i for i in self.settings.profiles.get("industries", [])
                      if re.search(rf"(?i)(?<!\w){re.escape(i)}(?!\w)", industry_text)]
        lang_section = sections.by_name.get("languages", "")
        langs: list[str] = []
        for lang in self.settings.profiles.get("spoken_languages", []):
            pat = re.compile(rf"(?i)(?<!\w){re.escape(lang)}(?!\w)")
            if lang_section and pat.search(lang_section):
                langs.append(lang)
            elif not lang_section:
                for m in pat.finditer(text):
                    window = text[max(0, m.start() - 40): m.end() + 40]
                    if _PROFICIENCY_RE.search(window):
                        langs.append(lang)
                        break
        if "Bahasa Indonesia" in langs and "Indonesian" in langs:
            langs.remove("Bahasa Indonesia")

        location = self.countries.detect("\n".join(sections.header_lines)) or ""
        profile = CandidateProfile(
            current_title=title,
            target_positions=targets[:10],
            adjacent_positions=adjacent[:8],
            experience_years=years,
            skills=skills,
            programming_languages=cats.get("programming_languages", []),
            frameworks=cats.get("frameworks", []),
            databases=cats.get("databases", []),
            tools=cats.get("tools", []),
            industry_experience=industries,
            projects=_short_lines(sections.by_name.get("projects", ""), 8),
            education=_short_lines(sections.by_name.get("education", ""), 5),
            certifications=_short_lines(sections.by_name.get("certifications", ""), 10),
            languages=langs,
            seniority_estimate=seniority,
            experience_areas=areas,
            role_families=[f for f, _, _ in fams],
            skill_evidence=evidence,
            location=location,
            analysis_method="deterministic",
            notes=notes,
            cv_filename=cv.filename,
            cv_sha256=cv.sha256,
        )
        if not skills:
            profile.notes.append("No known technologies were detected; consider adding a skills section to the CV.")
        return profile
