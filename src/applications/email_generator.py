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

from src.ai.prompts import application_email_prompt, redact_personal
from src.ai.provider_manager import AIManager
from src.applications.cv_document import CVDocument
from src.applications.fact_check import check_free_text
from src.applications.job_analyzer import JobAnalysis, detect_language
from src.applications.models import CandidateIdentity
from src.core.configuration import Settings
from src.core.exceptions import ConfigurationError
from src.core.models import JobOpportunity
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
        lang = language or self.language_for(job)
        tpl = self.templates.get(lang) or self.templates["en"]
        indonesian = lang == "id"
        years = profile.experience_years
        years_phrase = ""
        if years and years >= 1:
            y = int(years)
            more = years - y >= 0.25  # "over 4 years" only when it really is more than 4
            plus = bool(doc and re.search(rf"(?<!\d){y}\s*\+\s*(?:years?|yrs?|tahun)", doc.source_text, re.I))
            if indonesian:
                years_phrase = f" dengan pengalaman {y}+ tahun" if plus else                     f" dengan pengalaman {'lebih dari ' if more else ''}{y} tahun"
            else:
                years_phrase = f" with {y}+ years of experience" if plus else                     f" with {'over ' if more else ''}{y} year{'s' if y != 1 else ''} of experience"
        cv_skills = {s.lower() for s in profile.skills}
        skills = [s for s in (matched_skills or []) if s.lower() in cv_skills][:4]
        if not skills:
            skills = [s for s in job.required_skills if s.lower() in cv_skills][:4]
        skills_phrase = ""
        if skills:
            joined = ", ".join(skills[:-1]) + (f" {'dan' if indonesian else 'and'} " if len(skills) > 1 else "") + skills[-1]
            skills_phrase = f", terutama menggunakan {joined}" if indonesian else f", working mainly with {joined}"
        experience_phrase = ""
        if doc and doc.experience:
            e = doc.experience[0]
            if e.title and e.company:
                experience_phrase = (f"Pengalaman terakhir saya adalah sebagai {e.title} di {e.company}." if indonesian
                                     else f"Most recently, I have worked as {e.title} at {e.company}.")
        company = (job.company_name or "").strip()
        if indonesian:
            greeting = f"Yth. Tim Rekrutmen {company}," if company else "Yth. Tim Rekrutmen,"
        else:
            greeting = f"Dear Hiring Team at {company}," if company else "Dear Hiring Team,"
        values = {
            "greeting": greeting,
            "job_title": job.job_title.strip(),
            "company": company or ("perusahaan Anda" if indonesian else "your company"),
            "name": identity.name,
            "current_title": profile.current_title or ("profesional" if indonesian else "professional"),
            "years_phrase": years_phrase, "skills_phrase": skills_phrase, "experience_phrase": experience_phrase,
            "signature": signature(identity),
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
    async def ai_email(self, job: JobOpportunity, analysis: JobAnalysis, profile: CandidateProfile, doc: CVDocument,
                       matched_skills: list[str]) -> GeneratedEmail:
        fallback = self.template_email(job, profile, doc.identity, matched_skills=matched_skills, doc=doc,
                                       language=analysis.language)
        if self.ai is None or not self.ai.available:
            fallback.notes.append("AI unavailable: template e-mail used.")
            return fallback
        lang = fallback.language
        job_info = {"job_title": job.job_title, "company": job.company_name or "",
                    "requirements": analysis.requirements[:8], "responsibilities": analysis.responsibilities[:6],
                    "technologies": analysis.technologies[:15]}
        cand = {
            "current_title": profile.current_title, "experience_years": profile.experience_years,
            "summary": redact_personal(doc.summary)[:600],
            "matched_skills_with_cv_evidence": matched_skills[:10],
            "experience": [{"title": e.title, "company": e.company, "highlights": [redact_personal(b) for b in e.bullets[:4]]}
                           for e in doc.experience[:3]],
        }
        result = await self.ai.ask_json("application_email", application_email_prompt(job_info, cand, lang), cache_days=7)
        if not isinstance(result, dict):
            fallback.notes.append("AI e-mail unavailable: template e-mail used.")
            return fallback
        subject = re.sub(r"\s+", " ", str(result.get("subject") or "")).strip()[:150]
        body = _tidy(str(result.get("body") or ""))
        problems: list[str] = []
        years = {str(int(profile.experience_years))} if profile.experience_years else set()
        # The exact job title / company may be named, but technologies inside them are NOT thereby
        # claimed as skills: strip those strings, then check the rest against the CV only.
        checked = body + " " + subject
        for name in (job.job_title, job.company_name or ""):
            if name.strip():
                checked = re.sub(re.escape(name.strip()), " ", checked, flags=re.I)
        problems += check_free_text(checked, doc.source_text, self.detector, allowed_numbers=years)
        if job.job_title.lower() not in (body + " " + subject).lower():
            problems.append("does not mention the exact job title")
        if job.company_name and job.company_name.lower() not in body.lower():
            problems.append("does not mention the company")
        if not 250 <= len(body) <= 3000:
            problems.append(f"body length {len(body)} outside 250-3000 characters")
        if problems:
            fallback.notes.append("AI e-mail rejected by the fact check (" + "; ".join(problems) + "); template used.")
            return fallback
        closing = "Hormat saya," if lang == "id" else "Kind regards,"
        if not re.search(r"(?im)^(kind regards|best regards|regards|sincerely|hormat saya|salam)[,.]?\s*$", body):
            body = f"{body}\n\n{closing}"
        body = f"{body}\n{signature(doc.identity)}"
        if job.job_title.lower() not in subject.lower():
            subject = fallback.subject
        return GeneratedEmail(subject=subject, body=body, language=lang, method="ai", notes=[])
