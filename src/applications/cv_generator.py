"""Tailored CV generation (AI mode) and ATS-friendly PDF rendering.

The original CV is the single source of truth and is never modified:
* skills, bullets and projects are *reordered* by relevance to the job;
* the AI may *rephrase* bullets and the summary, but each rewrite must pass
  :mod:`src.applications.fact_check`, otherwise the original wording is kept;
* experience entries stay in the CV's own (chronological) order, and the headline stays the
  CV's own title (it never becomes the job title).
The PDF is a single-column, text-based layout with standard headings (ATS friendly),
rendered with PyMuPDF's Story API (no extra dependency) and validated after writing.
"""

from __future__ import annotations

import html
import io
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.ai.prompts import cv_tailor_prompt
from src.ai.provider_manager import AIManager
from src.applications.cv_document import CVDocument, ExperienceEntry
from src.applications.cv_matcher import MatchReport, _has
from src.applications.fact_check import check_free_text, check_rewrite
from src.applications.job_analyzer import JobAnalysis
from src.applications.models import CandidateIdentity
from src.core.configuration import Settings
from src.core.exceptions import JobIntelError
from src.extraction.content_parser import SkillDetector
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)


class CVGenerationError(JobIntelError):
    """The tailored CV could not be generated or failed validation."""


@dataclass
class TailoredCV:
    identity: CandidateIdentity
    headline: str
    summary: str
    skills: list[str]
    experience: list[ExperienceEntry]
    projects: list[str]
    education: list[str]
    certifications: list[str]
    languages: list[str]
    changes: list[str] = field(default_factory=list)
    rejected_rewrites: list[dict[str, Any]] = field(default_factory=list)
    method: str = "deterministic"
    skill_groups: list[tuple[str, list[str]]] = field(default_factory=list)
    project_entries: list[ExperienceEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_component(text: str | None, max_len: int = 50) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return t[:max_len].strip("_") or "Unknown"


def custom_cv_filename(company: str | None, title: str | None) -> str:
    return f"{safe_component(company)}_{safe_component(title)}_Custom_CV.pdf"


def _relevance(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(1 for t in terms if _has(low, t))


CSS = """
* { font-family: sans-serif; }
body { font-size: 10pt; line-height: 1.3; color: #000000; }
h1 { font-size: 18pt; margin: 0 0 2pt 0; }
p.headline { font-size: 11pt; margin: 0 0 2pt 0; }
p.contact { font-size: 9pt; margin: 0 0 8pt 0; }
h2 { font-size: 11.5pt; margin: 10pt 0 3pt 0; border-bottom: 1px solid #000000; }
p.role { font-weight: bold; margin: 6pt 0 0 0; }
p.dates { font-size: 9pt; margin: 0 0 2pt 0; }
ul { margin: 0 0 0 12pt; padding: 0; }
li { margin: 0 0 1.5pt 0; }
p { margin: 0 0 3pt 0; }
"""


class CVGenerator:
    def __init__(self, settings: Settings, ai: AIManager | None = None) -> None:
        self.settings = settings
        self.ai = ai
        self.detector = SkillDetector(settings.taxonomy)
        self.out_dir = settings.path("applications.generated_cv_directory", "data/generated_cvs")
        self.max_pages = int(settings.get("applications.ai.max_cv_pages", 3))
        self.rewrite = bool(settings.get("applications.ai.rewrite_with_ai", True))
        self.cache_days = 7.0  # 0 = bypass the AI response cache (used by "Regenerate CV")

    # --------------------------------------------------------------- tailor
    def tailor_deterministic(self, doc: CVDocument, analysis: JobAnalysis, match: MatchReport) -> TailoredCV:
        terms = list(dict.fromkeys([t.lower() for t in analysis.technologies + match.matched_keywords]))
        matched = [m["skill"] for m in match.matched_skills]
        skills = [s for s in doc.skills if s.lower() in {m.lower() for m in matched}]
        skills += [s for s in doc.skills if s not in skills]
        changes = []
        if skills[: len(matched)] != doc.skills[: len(matched)] and matched:
            changes.append(f"Skills reordered: job-relevant skills first ({', '.join(skills[:6])}).")
        experience = []
        for e in doc.experience:
            ordered = sorted(e.bullets, key=lambda b: -_relevance(b, terms))
            if ordered != e.bullets:
                changes.append(f"Bullets reordered by relevance in '{e.title}'.")
            experience.append(ExperienceEntry(e.title, e.company, e.dates, e.location, ordered))
        projects = sorted(doc.projects, key=lambda p: -_relevance(p, terms))
        if projects != doc.projects:
            changes.append("Projects reordered by relevance.")
        matched_lower = {m.lower() for m in matched}

        def item_rank(item: str) -> int:
            low = item.lower()
            return 0 if any(m in low or low in m for m in matched_lower) else (1 if _relevance(item, terms) else 2)

        # Skill groups as written in the CV: groups with job-relevant skills first, relevant items first.
        groups = [(label, sorted(items, key=item_rank)) for label, items in doc.skill_groups]
        groups.sort(key=lambda g: min((item_rank(i) for i in g[1]), default=2))
        if groups:
            skills = [i for _, items in groups for i in items]
        # Projects with bullets: most relevant first, bullets reordered.
        project_entries = sorted(
            (ExperienceEntry(p.title, p.company, p.dates, p.location, sorted(p.bullets, key=lambda b: -_relevance(b, terms)))
             for p in doc.project_entries),
            key=lambda p: -_relevance(" ".join([p.title, p.company, *p.bullets]), terms))
        return TailoredCV(identity=doc.identity, headline=doc.headline, summary=doc.summary, skills=skills,
                          experience=experience, projects=projects, education=list(doc.education),
                          certifications=list(doc.certifications), languages=list(doc.languages), changes=changes,
                          skill_groups=groups, project_entries=project_entries)

    async def tailor(self, doc: CVDocument, analysis: JobAnalysis, match: MatchReport, *,
                     experience_years: float | None = None) -> TailoredCV:
        tcv = self.tailor_deterministic(doc, analysis, match)
        if not self.rewrite or self.ai is None or not self.ai.available:
            if self.ai is not None and not self.ai.available:
                tcv.changes.append(f"AI rewriting not used ({self.ai.disabled_reason}); content is reordered only.")
            return tcv
        job = {k: v for k, v in analysis.to_dict().items() if k in (
            "job_title", "company", "required_skills", "preferred_skills", "responsibilities", "requirements", "keywords")}
        result = await self.ai.ask_json("cv_tailor", cv_tailor_prompt(job, doc.ai_safe_dict()), cache_days=self.cache_days)
        if not isinstance(result, dict):
            tcv.changes.append("AI tailoring unavailable for this job; content is reordered only.")
            return tcv
        tcv.method = "deterministic+ai"
        cv_text = doc.source_text
        # summary
        summary = str(result.get("summary") or "").strip()
        if summary:
            allowed_nums: set[str] = set()
            if self._years(doc):
                allowed_nums.add(str(self._years(doc)))
            if experience_years is not None and experience_years > 0:
                allowed_nums.add(str(int(experience_years)))
                allowed_nums.add(f"{experience_years:g}")
            problems = check_free_text(summary, cv_text, self.detector, allowed_numbers=allowed_nums, min_overlap=0.45)
            if problems or len(summary) > 700:
                tcv.rejected_rewrites.append({"item": "summary", "text": summary[:300],
                                              "problems": problems or ["too long"]})
            else:
                tcv.summary = summary
                tcv.changes.append("Summary rephrased for this job (validated against the CV).")
        # skills order (reorder only, matching exact/canonical/substring names)
        order = [str(s).strip() for s in result.get("skills_order") or [] if isinstance(s, str) and s.strip()]
        if order:
            def _order_rank(skill_item: str) -> int:
                low = skill_item.lower()
                canon_item = (self.detector.canonical(skill_item) or "").lower()
                for idx, target in enumerate(order):
                    tlow = target.lower()
                    tcanon = (self.detector.canonical(target) or "").lower()
                    if low == tlow or (canon_item and tcanon and canon_item == tcanon) or tlow in low or low in tlow:
                        return idx
                return len(order) + 1

            ranked_skills = sorted(tcv.skills, key=_order_rank)
            if ranked_skills != tcv.skills:
                tcv.skills = ranked_skills
            if tcv.skill_groups:
                tcv.skill_groups = [
                    (label, sorted(items, key=_order_rank))
                    for label, items in tcv.skill_groups
                ]
                tcv.skill_groups.sort(key=lambda g: min((_order_rank(x) for x in g[1]), default=len(order) + 1))
        # bullets
        sources: dict[str, tuple[int, str]] = {}
        for i, e in enumerate(doc.experience):
            for j, b in enumerate(e.bullets):
                sources[f"e{i}b{j}"] = (i, b)
        rewritten: dict[int, dict[str, str]] = {}
        for item in result.get("bullets") or []:
            if not isinstance(item, dict):
                continue
            bid, text = str(item.get("id") or ""), str(item.get("text") or "").strip()
            if bid not in sources or not text:
                continue
            idx, original = sources[bid]
            problems = check_rewrite(text, original, cv_text, self.detector)
            if problems:
                tcv.rejected_rewrites.append({"item": bid, "original": original, "text": text, "problems": problems})
                continue
            rewritten.setdefault(idx, {})[original] = text
        for idx, mapping in rewritten.items():
            entry = tcv.experience[idx]
            entry.bullets = [mapping.get(b, b) for b in entry.bullets]
            tcv.changes.append(f"{len(mapping)} bullet(s) rephrased in '{entry.title}' (validated against the CV).")
        if tcv.rejected_rewrites:
            tcv.changes.append(f"{len(tcv.rejected_rewrites)} AI rewrite(s) rejected by the fact check; "
                               "original wording kept.")
            log_event(log, 20, "cv.rewrites_rejected", count=len(tcv.rejected_rewrites))
        return tcv

    @staticmethod
    def _years(doc: CVDocument) -> int | None:
        m = re.search(r"(\d{1,2})\+?\s*(?:years|tahun)", doc.source_text, re.I)
        return int(m.group(1)) if m else None

    # --------------------------------------------------------------- render
    def to_html(self, t: TailoredCV) -> str:
        e = html.escape
        ident = t.identity
        contact = " | ".join(x for x in [ident.location, ident.email, ident.phone, *ident.links] if x)
        parts = [f"<h1>{e(ident.name or 'Curriculum Vitae')}</h1>"]
        if t.headline:
            parts.append(f'<p class="headline">{e(t.headline)}</p>')
        if contact:
            parts.append(f'<p class="contact">{e(contact)}</p>')
        if t.summary:
            parts.append(f"<h2>Summary</h2><p>{e(t.summary)}</p>")
        if t.skill_groups:
            parts.append("<h2>Skills</h2>")
            for label, items in t.skill_groups:
                if not label and not self.detector.detect_names(", ".join(items)):
                    label = "Soft Skills"
                prefix = f"<b>{e(label)}:</b> " if label else ""
                parts.append(f"<p>{prefix}{e(', '.join(items))}</p>")
        elif t.skills:
            parts.append(f"<h2>Skills</h2><p>{e(', '.join(t.skills))}</p>")
        if t.experience:
            parts.append("<h2>Experience</h2>")
            for x in t.experience:
                role = " - ".join(v for v in (x.title, x.company) if v)
                if role:
                    parts.append(f'<p class="role">{e(role)}</p>')
                if x.dates:
                    parts.append(f'<p class="dates">{e(x.dates)}</p>')
                if x.bullets:
                    parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in x.bullets) + "</ul>")
        if t.project_entries:
            parts.append("<h2>Projects</h2>")
            for x in t.project_entries:
                name = " - ".join(v for v in (x.title, x.company) if v)
                parts.append(f'<p class="role">{e(name)}</p>')
                if x.bullets:
                    parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in x.bullets) + "</ul>")
        for title, items in ((("Projects", t.projects),) if not t.project_entries else ()) + (("Education", t.education),
                             ("Certifications", t.certifications), ("Languages", t.languages)):
            if items:
                parts.append(f"<h2>{title}</h2><ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>")
        return "<body>" + "".join(parts) + "</body>"

    def render_pdf(self, t: TailoredCV, path: Path) -> Path:
        import pymupdf

        path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        try:
            story = pymupdf.Story(html=self.to_html(t), user_css=CSS)
            mediabox = pymupdf.paper_rect("a4")
            where = mediabox + (50, 45, -50, -45)
            writer = pymupdf.DocumentWriter(buffer)
            more, pages = 1, 0
            while more:
                dev = writer.begin_page(mediabox)
                more, _ = story.place(where)
                story.draw(dev)
                writer.end_page()
                pages += 1
                if pages > 10:
                    break
            writer.close()
            doc = pymupdf.open("pdf", buffer.getvalue())
            try:
                doc.set_metadata({"title": f"{t.identity.name or 'Candidate'} - CV", "author": t.identity.name or "",
                                  "creator": "AI Job Intelligence (tailored CV)", "producer": "PyMuPDF"})
                data = doc.tobytes(garbage=3, deflate=True)
            finally:
                doc.close()
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 - PyMuPDF raises several types
            raise CVGenerationError(f"PDF rendering failed: {exc}") from exc
        return path

    def validate_pdf(self, path: Path, t: TailoredCV) -> list[str]:
        import pymupdf

        problems: list[str] = []
        try:
            doc = pymupdf.open("pdf", path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            return [f"cannot open PDF: {exc}"]
        try:
            if doc.is_encrypted:
                problems.append("PDF is encrypted")
            if not 1 <= doc.page_count <= self.max_pages:
                problems.append(f"PDF has {doc.page_count} pages (allowed 1-{self.max_pages})")
            text = re.sub(r"\s+", " ", " ".join(p.get_text() for p in doc)).lower()
        finally:
            doc.close()
        if len(text) < 200:
            problems.append("PDF contains almost no extractable text (not ATS-readable)")
        if t.identity.name and t.identity.name.lower() not in text:
            problems.append("candidate name missing from PDF text")
        for heading, present in (("skills", t.skills), ("experience", t.experience)):
            if present and heading not in text:
                problems.append(f"section '{heading}' missing from PDF text")
        missing = [s for s in t.skills if s.lower() not in text]
        if t.skills and len(missing) > len(t.skills) * 0.2:
            problems.append(f"skills missing from PDF text: {', '.join(missing[:5])}")
        return problems

    def output_path(self, company: str | None, title: str | None, app_id: str) -> Path:
        path = self.out_dir / custom_cv_filename(company, title)
        if path.exists():
            path = path.with_name(path.stem + f"_{app_id[:6]}.pdf")
        return path

    async def generate(self, doc: CVDocument, analysis: JobAnalysis, match: MatchReport, *, company: str | None,
                       title: str | None, app_id: str, experience_years: float | None = None,
                       existing_path: Path | None = None) -> tuple[TailoredCV, Path]:
        tcv = await self.tailor(doc, analysis, match, experience_years=experience_years)
        final = Path(existing_path) if existing_path is not None else self.output_path(company, title, app_id)
        # Render and validate a candidate file first: an existing (possibly approved) CV is only
        # replaced by a new one that passed validation.
        candidate = self.render_pdf(tcv, final.with_name(final.stem + ".candidate.pdf"))
        problems = self.validate_pdf(candidate, tcv)
        if problems:
            candidate.unlink(missing_ok=True)
            raise CVGenerationError("Generated CV failed validation: " + "; ".join(problems))
        candidate.replace(final)
        return tcv, final
