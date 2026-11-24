"""Canonical structured CV document (shared by CV analysis and application tailoring).

Everything here is copied from the original CV text. The optional AI pass only helps
to segment messy CV text into entries; each AI-returned line must be found verbatim
(case/whitespace/bullet-insensitive) in the CV, otherwise it is discarded.
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

_BULLET_CHARS = r"•\-\*–·▪●◦»▸►‣⁃◆■➢➤✓✔→"
_BULLET = re.compile(rf"^\s*[{_BULLET_CHARS}]\s*")
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
    project_entries: list[ExperienceEntry] = field(default_factory=list)
    skill_groups: list[tuple[str, list[str]]] = field(default_factory=list)
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
        names = [self.identity.name] if self.identity.name else None
        return {
            "headline": self.headline,
            "summary": redact_personal(self.summary, extra_names=names),
            "experience": [
                {
                    "id": f"e{i}",
                    "title": e.title,
                    "company": e.company,
                    "dates": e.dates,
                    "bullets": [
                        {"id": f"e{i}b{j}", "text": redact_personal(b, extra_names=names)}
                        for j, b in enumerate(e.bullets)
                    ],
                }
                for i, e in enumerate(self.experience)
            ],
            "skills": self.skills,
            "projects": [redact_personal(p, extra_names=names) for p in self.projects],
            "project_entries": [
                {
                    "title": p.title,
                    "company": p.company,
                    "dates": p.dates,
                    "bullets": [redact_personal(b, extra_names=names) for b in p.bullets],
                }
                for p in self.project_entries
            ],
            "education": self.education,
            "certifications": self.certifications,
            "languages": self.languages,
        }


# ------------------------------------------------------------------ identity
def _nice_name(name: str) -> str:
    """'ROHMAT SUPRIYADI' -> 'Rohmat Supriyadi' (only when the CV writes the name in capitals)."""
    return " ".join(w.capitalize() for w in name.split()) if name.isupper() else name


def display_link(url: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", url.strip()).split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def extract_identity(text: str, profile: CandidateProfile | None = None,
                     file_links: list[str] | None = None) -> CandidateIdentity:
    header = [l.strip() for l in text.splitlines()[:12] if l.strip()]
    ident = CandidateIdentity()
    for line in header[:3]:
        words = line.split()
        if 2 <= len(words) <= 5 and not re.search(r"[\d@|/:]", line) and not ROLE_WORDS.search(line):
            ident.name = _nice_name(line.strip())
            break
    joined = "\n".join(header)
    m = EMAIL_RE.search(joined)
    ident.email = m.group(0) if m else ""
    for pm in _PHONE.finditer(joined):
        if not re.fullmatch(r"\d{4}\s*[-–]\s*\d{4}", pm.group(0).strip()):
            ident.phone = pm.group(0).strip()
            break
    text_links = [l.group(0).rstrip(".,;") for l in _LINK.finditer(joined)]
    ident.links = list(dict.fromkeys(display_link(u) for u in (file_links or []) + text_links
                                     if not u.lower().startswith("mailto:")))[:4]
    ident.location = (profile.location if profile else "") or ""
    for line in header[1:4]:
        for seg in re.split(r"\s*\|\s*", line):
            if profile and profile.location and profile.location.lower() in seg.lower() and len(seg) < 60:
                ident.location = seg.strip()
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
        if ROLE_WORDS.search(b) and not ROLE_WORDS.search(a):
            return b, a
        return a, b
    return line.strip(), ""


def parse_experience(section: str) -> list[ExperienceEntry]:
    """Split an experience/projects section into entries with bullets.

    * Bulleted CVs: every non-bullet line that is not a wrapped continuation is an entry header.
    * Unbulleted CVs: a header is a short line with role words or followed by a date line.
    * Wrapped lines (indented, or starting lower-case / with punctuation) continue the previous bullet,
      except when the line itself matches a header pattern (date range or role + separator).
    """
    entries: list[ExperienceEntry] = []
    cur: ExperienceEntry | None = None
    lines = [l.rstrip() for l in section.splitlines()]
    bulleted = sum(1 for l in lines if _BULLET.match(l.strip())) >= 2
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        is_bullet = bool(_BULLET.match(line))
        looks_like_header = bool(_RANGE_RE.search(line)) or (
            bool(ROLE_WORDS.search(line)) and bool(_SPLIT_TITLE.search(line)) and len(line) <= 140
        )
        continuation = (
            not is_bullet
            and not looks_like_header
            and (raw[:1].isspace() or line[:1].islower() or line[:1] in "(,;&")
        )
        if continuation and cur is not None and cur.bullets:
            cur.bullets[-1] = f"{cur.bullets[-1]} {line}"
            continue
        text = _BULLET.sub("", line).strip()
        rng = _RANGE_RE.search(text)
        rest = _RANGE_RE.sub("", text).strip(" ,|-–—()") if rng else text
        if not is_bullet and rng and len(rest) < 4 and cur is not None:
            cur.dates = rng.group(0).strip()
            continue
        if bulleted:
            header_like = not is_bullet and len(text) <= 160
        else:
            next_line = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
            header_like = (
                not is_bullet
                and len(text) <= 120
                and not text.endswith(".")
                and (
                    ROLE_WORDS.search(rest) is not None
                    or (bool(_RANGE_RE.search(next_line)) and len(_RANGE_RE.sub("", next_line).strip()) < 4)
                )
            )
        if header_like:
            title, company = _split_title_company(rest)
            cur = ExperienceEntry(title=title, company=company, dates=rng.group(0).strip() if rng else "")
            entries.append(cur)
            continue
        if cur is None:
            cur = ExperienceEntry()
            entries.append(cur)
        cur.bullets.append(text)
    return [e for e in entries if e.title or e.bullets]


def _split_outside_parens(text: str) -> list[str]:
    out, depth, buf = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        if depth == 0 and ch in ",;•|":
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    return [x.strip(" .") for x in out if x.strip(" .")]


_GROUP_LABEL = re.compile(
    r"(?i)\b(languages?|frontend|front-end|backend|back-end|database|databases|tools?|devops|"
    r"cloud|api|ui/ux|frameworks?|testing|mobile|data|others?|skills?|cms|integration|"
    r"performance|libraries)\b"
)


def parse_skills(section: str, detector: Any) -> list[tuple[str, list[str]]]:
    """'Backend Node.js, Express.js, NestJS' -> ('Backend', ['Node.js', 'Express.js', 'NestJS'])."""
    logical: list[str] = []
    for raw in section.splitlines():
        if not raw.strip():
            continue
        if logical and (raw[:1].isspace() or raw.strip()[:1].islower()):
            logical[-1] += " " + raw.strip()
        else:
            logical.append(_BULLET.sub("", raw.strip()))
    groups: list[tuple[str, list[str]]] = []
    for line in logical:
        label = ""
        head = line.split(",", 1)[0]
        if ":" in head:
            label, line = line.split(":", 1)
        elif detector is not None:
            low = line.lower()
            positions = [low.find(h.name.lower()) for h in detector.detect(head)]
            positions += [m.start() for m in re.finditer(r"\b[A-Z][\w.+#-]*\.js\b", head)]
            first = min((p for p in positions if p > 0), default=-1)
            if first > 0 and len(line[:first].split()) <= 4 and _GROUP_LABEL.search(line[:first]):
                label, line = line[:first], line[first:]
        items = [i for i in _split_outside_parens(line) if 1 <= len(i) <= 60]
        if items:
            groups.append((label.strip(" :-"), items))
    return groups


def _items(section: str, limit: int, max_len: int = 200) -> list[str]:
    """One item per logical line (wrapped/indented lines are joined to the previous one)."""
    out: list[str] = []
    for raw in section.splitlines():
        l = _BULLET.sub("", raw.strip()).strip()
        if not l:
            continue
        if out and (raw[:1].isspace() or l[:1].islower()):
            out[-1] = f"{out[-1]} {l}"
        elif l not in out:
            out.append(l)
    return [o for o in out if 3 <= len(o) <= max_len][:limit]


def structure_cv(cv: ParsedCV, profile: CandidateProfile, detector: Any = None) -> CVDocument:
    sections: Sections = split_sections(cv.text)
    by = sections.by_name
    groups = parse_skills(by.get("skills", ""), detector or _default_detector())
    skills = list(profile.skills)
    lower = {s.lower() for s in skills}
    for _, items in groups:
        for item in items:
            if item.lower() not in lower:
                skills.append(item)
                lower.add(item.lower())
    projects = parse_experience(by.get("projects", ""))
    project_names = [" - ".join(v for v in (p.title, p.company) if v) for p in projects if p.title]
    doc = CVDocument(
        identity=extract_identity(cv.text, profile, getattr(cv, "links", None)),
        headline=profile.current_title,
        summary=" ".join(l.strip() for l in by.get("summary", "").splitlines() if l.strip()),
        experience=parse_experience(by.get("experience", "")),
        skills=skills,
        education=_items(by.get("education", ""), 6) or list(profile.education),
        projects=project_names or _items(by.get("projects", ""), 8) or list(profile.projects),
        certifications=_items(by.get("certifications", ""), 10) or list(profile.certifications),
        languages=_items(by.get("languages", ""), 5) or list(profile.languages),
        project_entries=[p for p in projects if p.title and p.bullets],
        skill_groups=groups,
        source_text=cv.text,
        source_sha256=cv.sha256,
    )
    if not doc.experience:
        doc.notes.append("No work-experience entries could be segmented from the CV text.")
    return doc


_DETECTOR: list[Any] = []


def _default_detector() -> Any:
    """SkillDetector over the project taxonomy (loaded once); None if unavailable."""
    if not _DETECTOR:
        try:
            from src.core.configuration import load_settings
            from src.extraction.content_parser import SkillDetector

            _DETECTOR.append(SkillDetector(load_settings(load_env=False).taxonomy))
        except Exception:  # noqa: BLE001 - grouping degrades gracefully
            _DETECTOR.append(None)
    return _DETECTOR[0]


def _verbatim_norm(text: str) -> str:
    text = re.sub(rf"(?m)^\s*[{_BULLET_CHARS}]\s*", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _verbatim(value: str, cv_norm: str) -> bool:
    v = _verbatim_norm(value)
    return len(v) >= 2 and v in cv_norm


async def structure_cv_ai(cv: ParsedCV, profile: CandidateProfile, ai: AIManager | None) -> CVDocument:
    """Deterministic structure, replaced by an AI segmentation only when every line is verified."""
    doc = structure_cv(cv, profile)
    if ai is None or not ai.available:
        return doc
    names = [doc.identity.name] if doc.identity.name else None
    result = await ai.ask_json(
        "cv_structure",
        cv_structure_prompt(redact_personal(cv.text[:30000], extra_names=names)),
        cache_days=30,
    )
    if not isinstance(result, dict):
        return doc
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
        entries.append(
            ExperienceEntry(
                title=title,
                company=company,
                dates=dates if dates and _verbatim(dates, cv_norm) else "",
                location="",
                bullets=bullets,
            )
        )
    total_bullets = sum(len(e.bullets) for e in entries)
    if entries and total_bullets >= sum(len(e.bullets) for e in doc.experience):
        doc.experience = entries
        doc.method = "deterministic+ai_segmentation"
    if dropped:
        doc.notes.append(f"{dropped} AI-segmented item(s) discarded: not found verbatim in the CV.")
    return doc
