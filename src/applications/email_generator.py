"""Application e-mail generation.

* Template e-mails (Basic mode and fallback) are filled only with facts taken from the CV.
* AI e-mails (AI mode) are checked by :mod:`src.applications.fact_check`; if a check fails the
  template e-mail is used instead. Contact details are never written by the AI: the signature
  is appended locally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.ai.prompts import application_email_prompt, cover_note_prompt, redact_personal
from src.ai.provider_manager import AIManager
from src.applications.fact_check import check_free_text
from src.applications.job_analyzer import JobAnalysis, detect_language
from src.applications.models import CandidateIdentity
from src.core.configuration import Settings
from src.core.exceptions import ConfigurationError
from src.core.models import JobOpportunity
from src.cv.document import CVDocument
from src.cv.models import CandidateProfile
from src.extraction.content_parser import SkillDetector

DEFAULT_TEMPLATES = {
    "en": {
        "subject": "Application for {job_title} - {name}",
        "body": "{greeting}\n\nI am writing to apply for the {job_title} position. I am a "
                "{current_title}{years_phrase}{skills_phrase}.\n\n{experience_phrase}\n\nPlease find my CV attached. "
                "Thank you for your time and consideration.\n\nKind regards,\n{signature}",
    }
}


@dataclass
class GeneratedEmail:
    subject: str
    body: str
    language: str = "en"
    method: str = "template"
    notes: list[str] = field(default_factory=list)


def signature(identity: CandidateIdentity) -> str:
    lines = [identity.name, identity.phone, identity.email, *identity.links[:2]]
    return "\n".join(l for l in lines if l)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+([.,])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(l.rstrip() for l in text.strip().splitlines())


_STOP_TERMS = set("senior junior lead staff principal engineer developer software position role team with and the for "
                  "remote hybrid onsite full time part".split())
_VERB_START = re.compile(r"^(?:[A-Z][a-z]+ed|Built|Led|Ran|Made|Wrote|Set|Grew|Cut|Drove|Won|Took|Kept|Held|Rebuilt|"
                         r"Redesigned|Rewrote|Shipped|Launched|Owned|Ported|Moved|Brought|Taught|Ran)\b")
_ID_VERB_START = re.compile(
    r"^(?:Membangun|Mengembangkan|Merancang|Mengelola|Memimpin|Membuat|Mengoptimalkan|Mengintegrasikan|"
    r"Meningkatkan|Menangani|Menyusun|Memperbaiki|Memelihara|Mengimplementasikan)\b"
)


def _article(noun: str) -> str:
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _join(items: list[str], indonesian: bool) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + (" dan " if indonesian else " and ") + items[-1]


def _mentions(text: str, skill: str) -> bool:
    return bool(text) and bool(re.search(rf"(?i)(?<![\w+#]){re.escape(skill)}(?:\.js)?(?![\w+#])", text))


def _job_terms(job: JobOpportunity) -> list[str]:
    words = [w for w in re.findall(r"[a-z][a-z0-9+#.]{3,}", job.job_title.lower()) if w not in _STOP_TERMS]
    return list(dict.fromkeys([s.lower() for s in job.required_skills + job.preferred_skills] + words))


_CLAUSE_CUT = re.compile(r",\s+(?:accompanied|while|which|featuring|supported|resulting|ensuring|enabling|"
                         r"leading|allowing|making|thereby|helping|in order)\b")


def _shorten(bullet: str) -> str:
    """Cut a long CV bullet at its first trailing clause (no words are added or changed)."""
    if len(bullet) <= 150:
        return bullet
    m = _CLAUSE_CUT.search(bullet, 60)
    return bullet[:m.start()].rstrip(" ,;") + "." if m else bullet


def _nice(name: str) -> str:
    return " ".join(w.capitalize() if len(w) > 3 else w for w in name.split()) if name.isupper() else name


def _achievement_sentence(doc: CVDocument | None, terms: list[str], current: Any, headline: str = "") -> str:
    """One verbatim CV bullet relevant to the job, as 'In my current role, I built ...'."""
    if doc is None:
        return ""
    candidates: list[tuple[int, int, str, str]] = []
    for idx, e in enumerate(doc.experience[:2]):
        if idx == 0 and current is not None and re.search(r"(?i)present|current|now", e.dates or ""):
            lead = "In my current role, I "
        elif e.title and e.company and headline and e.title.lower() == headline.lower():
            lead = f"In my previous role at {e.company}, I "
        elif e.title and e.company:
            lead = f"As {_article(e.title)} {e.title} at {e.company}, I "
        else:
            lead = ""
        for b in e.bullets:
            candidates.append((_score(b, terms), idx, lead, b))
    for p in doc.project_entries[:4]:
        for b in p.bullets:
            name = _nice(p.title)
            label = name if name.lower().endswith(("project", "system", "platform", "app")) else f"{name} project"
            candidates.append((_score(b, terms), 5, f"On the {label}, I ", b))
    best = [c for c in candidates if c[0] > 0 and c[2] and len(c[3]) <= 260 and _VERB_START.match(c[3])]
    if not best:
        return ""
    best.sort(key=lambda c: (-c[0], c[1], len(c[3])))
    _, _, lead, bullet = best[0]
    bullet = _shorten(bullet)
    sentence = lead + bullet[0].lower() + bullet[1:]
    return sentence if sentence.endswith((".", "!", ")")) else sentence + "."


def _achievement_sentence_id(doc: CVDocument | None, terms: list[str], current: Any) -> str:
    """One verbatim Indonesian CV bullet relevant to the job (only when the bullet is in Indonesian)."""
    if doc is None:
        return ""
    candidates: list[tuple[int, int, str, str]] = []
    for idx, e in enumerate(doc.experience[:2]):
        if idx == 0 and current is not None and re.search(r"(?i)present|current|now|sekarang|saat\s+ini", e.dates or ""):
            lead = "Di peran saya saat ini, saya "
        elif e.company:
            lead = f"Di peran saya di {e.company}, saya "
        else:
            lead = ""
        for b in e.bullets:
            candidates.append((_score(b, terms), idx, lead, b))
    for p in doc.project_entries[:4]:
        for b in p.bullets:
            candidates.append((_score(b, terms), 5, f"Pada proyek {_nice(p.title)}, saya ", b))
    best = [c for c in candidates if c[0] > 0 and c[2] and len(c[3]) <= 260 and _ID_VERB_START.match(c[3])]
    if not best:
        return ""
    best.sort(key=lambda c: (-c[0], c[1], len(c[3])))
    _, _, lead, bullet = best[0]
    sentence = lead + bullet[0].lower() + bullet[1:]
    return sentence if sentence.endswith((".", "!", ")")) else sentence + "."


def _score(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(1 for t in terms if re.search(rf"(?<![\w+#]){re.escape(t)}(?![\w+#])", low))


class EmailGenerator:
    def __init__(self, settings: Settings, ai: AIManager | None = None) -> None:
        self.settings = settings
        self.ai = ai
        self.detector = SkillDetector(settings.taxonomy)
        path = settings.path("applications.email_templates_file", "config/email_templates.yaml")
        self.templates: dict[str, Any] = DEFAULT_TEMPLATES
        if Path(path).exists():
            with open(path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if isinstance(loaded, dict) and loaded.get("en"):
                self.templates = loaded
        self.default_language = str(settings.get("applications.email_language", "auto"))

    def language_for(self, job: JobOpportunity) -> str:
        if self.default_language in self.templates:
            return self.default_language
        lang = detect_language(f"{job.job_title}\n{job.job_description}")
        return lang if lang in self.templates else "en"

    # ------------------------------------------------------------- template
    def template_email(self, job: JobOpportunity, profile: CandidateProfile, identity: CandidateIdentity, *,
                       matched_skills: list[str] | None = None, doc: CVDocument | None = None,
                       language: str | None = None) -> GeneratedEmail:
        """Plain, human e-mail. Every sentence is built from a fact in the CV:
        current/most recent role (from the experience dates), the CV's own years statement, one real
        CV bullet that fits the job, and job skills that the CV shows in actual work."""
        lang = language or self.language_for(job)
        tpl = self.templates.get(lang) or self.templates["en"]
        indonesian = lang == "id"
        company = (job.company_name or "").strip()
        headline = profile.current_title or ""

        # ---- years: the CV's own wording ("3+ years") when present, never rounded up
        years_text = ""
        years = profile.experience_years
        if years and years >= 1:
            y = int(years)
            plus = bool(doc and re.search(rf"(?<!\d){y}\s*\+\s*(?:years?|yrs?|tahun)", doc.source_text, re.I))
            more = years - y >= 0.25
            years_text = f"{y}+" if plus else (f"lebih dari {y}" if indonesian and more else
                                                (f"over {y}" if more else f"{y}"))

        # ---- current / most recent role, from the experience entry dates
        entry = doc.experience[0] if doc and doc.experience else None
        role_sentence = ""
        if entry and entry.title and entry.company:
            current = bool(re.search(r"(?i)present|current|now|sekarang|saat\s+ini", entry.dates or ""))
            same_title = bool(headline and entry.title.strip().lower() == headline.strip().lower())
            if indonesian:
                role_sentence = (f"Saat ini saya bekerja sebagai {entry.title} di {entry.company}" if current else
                                 f"Terakhir saya bekerja sebagai {entry.title} di {entry.company}")
                if years_text:
                    if headline and not same_title:
                        role_sentence += f", dengan pengalaman {years_text} tahun sebagai {headline}"
                    else:
                        role_sentence += f", dengan pengalaman {years_text} tahun"
                role_sentence += "."
            else:
                role_sentence = (f"I'm currently {_article(entry.title)} {entry.title} at {entry.company}" if current
                                 else f"Most recently, I worked as {_article(entry.title)} {entry.title} at {entry.company}")
                if years_text:
                    if headline and not same_title:
                        role_sentence += f", and I have {years_text} years of experience as {_article(headline)} {headline}"
                    else:
                        role_sentence += f", with {years_text} years of experience"
                role_sentence += "."
        elif headline:
            role_sentence = (f"Saya seorang {headline}" + (f" dengan pengalaman {years_text} tahun" if years_text else "")
                             + ".") if indonesian else \
                (f"I'm {_article(headline)} {headline}" + (f" with {years_text} years of experience" if years_text else "")
                 + ".")

        # ---- one real CV bullet that fits the job (verbatim)
        terms = _job_terms(job)
        achievement = (
            _achievement_sentence_id(doc, terms, entry)
            if indonesian
            else _achievement_sentence(doc, terms, entry, headline)
        )

        # ---- skills the job asks for AND the CV shows in actual work (bullets), else only "listed"
        cv_skills = {s.lower() for s in profile.skills}
        wanted = [s for s in (list(matched_skills or []) + list(job.required_skills) + list(job.preferred_skills))
                  if s.lower() in cv_skills]
        wanted = list(dict.fromkeys(wanted))
        work_text = " ".join(b for e in ((doc.experience + doc.project_entries) if doc else []) for b in e.bullets)
        used = [s for s in wanted if _mentions(work_text, s) and not _mentions(achievement, s)][:3]
        listed = [s for s in wanted if s not in used and not _mentions(achievement, s)][:3]
        skills_sentence = ""
        if used:
            joined = _join(used, indonesian)
            skills_sentence = (f"Saya sudah menggunakan {joined} secara langsung dalam pekerjaan saya, sesuai dengan "
                               "kebutuhan posisi ini." if indonesian else
                               f"I've worked hands-on with {joined}, which {'are' if len(used) > 1 else 'is'} part of "
                               "this role's requirements.")
        elif listed:
            joined = _join(listed, indonesian)
            skills_sentence = (f"Keahlian saya juga mencakup {joined}." if indonesian else
                               f"My skill set also includes {joined}.")

        if indonesian:
            greeting = f"Yth. Tim Rekrutmen {company}," if company else "Yth. Tim Rekrutmen,"
        else:
            greeting = f"Dear {company} Hiring Team," if company else "Dear Hiring Team,"
        values = {
            "greeting": greeting,
            "job_title": job.job_title.strip(),
            "company": company or ("perusahaan Anda" if indonesian else "your company"),
            "name": identity.name,
            "role_sentence": role_sentence,
            "achievement_sentence": achievement,
            "skills_sentence": skills_sentence,
            "signature": signature(identity),
            # placeholders of the earlier templates (custom templates keep working)
            "current_title": headline or ("profesional" if indonesian else "professional"),
            "years_phrase": (f" dengan pengalaman {years_text} tahun" if indonesian else
                             f" with {years_text} years of experience") if years_text else "",
            "skills_phrase": "", "experience_phrase": "",
        }
        try:
            subject = _tidy(str(tpl["subject"]).format(**values))
            body = _tidy(str(tpl["body"]).format(**values))
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigurationError(f"E-mail template '{lang}' has an unknown placeholder or brace: {exc}. "
                                     f"Allowed: {', '.join('{' + k + '}' for k in values)}") from exc
        subject = re.sub(r"\s*[-–|]\s*$", "", subject)  # "Application for X - " when the name is unknown
        notes = [] if job.company_name else ["Company name unknown: a generic greeting is used."]
        return GeneratedEmail(subject=subject, body=body, language=lang, method="template", notes=notes)

    # ------------------------------------------------------------------- AI
    def _ai_candidate_payload(self, profile: CandidateProfile, doc: CVDocument,
                              matched_skills: list[str]) -> dict[str, Any]:
        names = [doc.identity.name] if doc.identity.name else None
        projects = [
            {
                "title": p.title,
                "company": p.company,
                "dates": p.dates,
                "highlights": [redact_personal(b, extra_names=names) for b in p.bullets[:3]],
            }
            for p in doc.project_entries[:3]
        ] or [{"title": redact_personal(p, extra_names=names)} for p in doc.projects[:4]]
        return {
            "current_title": profile.current_title,
            "experience_years": profile.experience_years,
            "summary": redact_personal(doc.summary, extra_names=names)[:600],
            "matched_skills_with_cv_evidence": matched_skills[:10],
            "experience": [
                {
                    "title": e.title,
                    "company": e.company,
                    "dates": e.dates,
                    "highlights": [redact_personal(b, extra_names=names) for b in e.bullets[:4]],
                }
                for e in doc.experience[:3]
            ],
            "projects": projects,
        }

    def _effective_language(self, analysis: JobAnalysis) -> str | None:
        """Honor configured email_language ('en'|'id'); use analysis.language only when 'auto'."""
        if self.default_language in self.templates:
            return self.default_language
        return analysis.language if analysis.language in self.templates else None

    async def ai_email(self, job: JobOpportunity, analysis: JobAnalysis, profile: CandidateProfile, doc: CVDocument,
                       matched_skills: list[str]) -> GeneratedEmail:
        fallback = self.template_email(job, profile, doc.identity, matched_skills=matched_skills, doc=doc,
                                       language=self._effective_language(analysis))
        if self.ai is None or not self.ai.available:
            fallback.notes.append("AI unavailable: template e-mail used.")
            return fallback
        lang = fallback.language
        job_info = {"job_title": job.job_title, "company": job.company_name or "",
                    "requirements": analysis.requirements[:8], "responsibilities": analysis.responsibilities[:6],
                    "technologies": analysis.technologies[:15]}
        cand = self._ai_candidate_payload(profile, doc, matched_skills)
        result = await self.ai.ask_json("application_email", application_email_prompt(job_info, cand, lang), cache_days=7)
        if not isinstance(result, dict):
            fallback.notes.append("AI e-mail unavailable: template e-mail used.")
            return fallback
        subject = re.sub(r"\s+", " ", str(result.get("subject") or "")).strip()[:150]
        body = _tidy(str(result.get("body") or ""))
        problems: list[str] = []
        years = {str(int(profile.experience_years))} if profile.experience_years else set()
        # Strip job title and company name before fact- and tone-checking so employer/role wording
        # (e.g. "Dynamic", "Seamless", or required technologies in the title) never triggers false positives.
        checked = body + " " + subject
        for name in (job.job_title, job.company_name or ""):
            if name.strip():
                checked = re.sub(re.escape(name.strip()), " ", checked, flags=re.I)
        problems += check_free_text(checked, doc.source_text, self.detector, allowed_numbers=years)
        if job.job_title.lower() not in (body + " " + subject).lower():
            problems.append("does not mention the exact job title")
        if job.company_name and job.company_name.lower() not in body.lower():
            problems.append("does not mention the company")
        if not 140 <= len(body) <= 3000:
            problems.append(f"body length {len(body)} outside 140-3000 characters")
        if problems:
            fallback.notes.append("AI e-mail rejected by the fact check (" + "; ".join(problems) + "); template used.")
            return fallback
        closing = "Hormat saya," if lang == "id" else "Best regards,"
        if not re.search(r"(?im)^(kind regards|best regards|regards|sincerely|hormat saya|salam)[,.]?\s*$", body):
            body = f"{body}\n\n{closing}"
        body = f"{body}\n{signature(doc.identity)}"
        if job.job_title.lower() not in subject.lower():
            subject = fallback.subject
        return GeneratedEmail(subject=subject, body=body, language=lang, method="ai", notes=[])

    # ------------------------------------------------------------ web cover note
    async def cover_note(self, job: JobOpportunity, analysis: JobAnalysis, profile: CandidateProfile, doc: CVDocument,
                         matched_skills: list[str], *, use_ai: bool = True) -> GeneratedEmail:
        """Concise, paste-ready cover note for Web/ATS application forms (channel='web')."""
        base = self.template_email(job, profile, doc.identity, matched_skills=matched_skills, doc=doc,
                                   language=self._effective_language(analysis))
        lang = base.language
        indonesian = lang == "id"
        # Convert the e-mail template into a clean form-ready cover note (no "attached" / signature headers).
        lines = [l for l in base.body.splitlines() if not l.strip().lower().startswith(("dear ", "yth."))]
        trimmed_lines: list[str] = []
        for l in lines:
            if re.match(r"(?i)^(best regards|kind regards|hormat saya|sincerely|regards)[,.]?\s*$", l.strip()):
                break
            if re.search(r"(?i)(cv is attached|cv saya terlampir|please find my cv attached)", l):
                continue
            trimmed_lines.append(l)
        fallback_body = _tidy("\n".join(trimmed_lines))
        company = (job.company_name or "").strip()
        if company and company.lower() not in fallback_body.lower():
            if indonesian:
                fallback_body = re.sub(
                    rf"(posisi\s+{re.escape(job.job_title.strip())})\.",
                    rf"\1 di {company}.",
                    fallback_body,
                    count=1,
                )
            else:
                fallback_body = re.sub(
                    rf"(the\s+{re.escape(job.job_title.strip())}\s+position)\.",
                    rf"\1 at {company}.",
                    fallback_body,
                    count=1,
                )
        if not fallback_body:
            fallback_body = base.body
        subject = f"Cover Note: {job.job_title.strip()}" + (f" - {job.company_name.strip()}" if job.company_name else "")
        fallback = GeneratedEmail(subject=subject[:150], body=fallback_body, language=lang, method="template",
                                  notes=list(base.notes))
        if not use_ai or self.ai is None or not self.ai.available:
            if use_ai and (self.ai is None or not self.ai.available):
                fallback.notes.append("AI unavailable: template cover note used.")
            return fallback
        job_info = {"job_title": job.job_title, "company": job.company_name or "",
                    "requirements": analysis.requirements[:8], "responsibilities": analysis.responsibilities[:6],
                    "technologies": analysis.technologies[:15]}
        cand = self._ai_candidate_payload(profile, doc, matched_skills)
        result = await self.ai.ask_json("cover_note", cover_note_prompt(job_info, cand, lang), cache_days=7)
        if not isinstance(result, dict):
            fallback.notes.append("AI cover note unavailable: template cover note used.")
            return fallback
        body = _tidy(str(result.get("body") or ""))
        problems: list[str] = []
        years = {str(int(profile.experience_years))} if profile.experience_years else set()
        checked = body
        for name in (job.job_title, job.company_name or ""):
            if name.strip():
                checked = re.sub(re.escape(name.strip()), " ", checked, flags=re.I)
        problems += check_free_text(checked, doc.source_text, self.detector, allowed_numbers=years)
        if job.job_title.lower() not in body.lower():
            problems.append("does not mention the exact job title")
        if job.company_name and job.company_name.lower() not in body.lower():
            problems.append("does not mention the company")
        if not 100 <= len(body) <= 2500:
            problems.append(f"body length {len(body)} outside 100-2500 characters")
        if problems:
            fallback.notes.append("AI cover note rejected by the fact check (" + "; ".join(problems) + "); template used.")
            return fallback
        return GeneratedEmail(subject=subject[:150], body=body, language=lang, method="ai", notes=[])
