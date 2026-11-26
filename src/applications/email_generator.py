"""Application e-mail generation.

* Template e-mails (Basic mode and AI fallback) are built strictly from verified CV facts and
  structured job analysis, tailored by role focus (Fullstack, Backend, Frontend, AI Integration,
  Software Engineer) without unnecessary employer name-dropping or repetitive boilerplate.
* AI e-mails (AI mode) pass rich, PII-redacted job + candidate context to the LLM and verify
  every generated output through :mod:`src.applications.fact_check`; if any check fails, the
  deterministic e-mail is used instead. Contact details and links are always appended locally.
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
        "subject": "{subject_line}",
        "body": (
            "{greeting}\n\n{opening_sentence}\n\n{role_sentence}\n\n"
            "{achievement_sentence}\n\n{portfolio_sentence} {closing_sentence}\n\n"
            "{sign_off}\n{signature}"
        ),
    }
}


@dataclass
class GeneratedEmail:
    subject: str
    body: str
    language: str = "en"
    method: str = "template"
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ formatting & links
def _classify_link(url: str) -> tuple[int, str, str]:
    """Return (sort_priority, label, clean_url) for a verified candidate link."""
    clean = url.strip()
    low = clean.lower()
    if "github.com" in low:
        return (1, "GitHub", clean)
    if "gitlab.com" in low:
        return (1, "GitLab", clean)
    if "linkedin.com" in low:
        return (2, "LinkedIn", clean)
    if "figma.com" in low:
        return (4, "Figma", clean)
    return (0, "Portfolio", clean)


def _select_signature_links(links: list[str], *, include_figma: bool = False) -> list[tuple[str, str]]:
    """Return up to 3 valid labeled links ordered: Portfolio, GitHub, LinkedIn."""
    parsed: list[tuple[int, str, str]] = []
    seen_urls: set[str] = set()
    for raw in links or []:
        u = (raw or "").strip()
        if not u or "." not in u or " " in u or u.lower() in seen_urls:
            continue
        seen_urls.add(u.lower())
        prio, label, clean = _classify_link(u)
        if label == "Figma" and not include_figma:
            continue
        parsed.append((prio, label, clean))
    parsed.sort(key=lambda item: item[0])
    # Keep distinct primary categories first (Portfolio, GitHub, LinkedIn)
    selected: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    for _, label, clean in parsed:
        if label in seen_labels and len(parsed) > 3:
            continue
        seen_labels.add(label)
        selected.append((label, clean))
        if len(selected) >= 3:
            break
    return selected


def signature(identity: CandidateIdentity, *, include_figma: bool = False) -> str:
    lines = [identity.name, identity.phone, identity.email]
    for label, url in _select_signature_links(identity.links, include_figma=include_figma):
        lines.append(f"{label}: {url}")
    return "\n".join(l for l in lines if l)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+([.,])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(l.rstrip() for l in text.strip().splitlines())


def _article(noun: str) -> str:
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _join(items: list[str], indonesian: bool) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {'dan' if indonesian else 'and'} {items[1]}"
    return ", ".join(items[:-1]) + (", dan " if indonesian else ", and ") + items[-1]


def _mentions(text: str, term: str) -> bool:
    if not text or not term:
        return False
    return bool(re.search(rf"(?i)(?<![\w+#]){re.escape(term)}(?:s|\.js)?(?![\w+#])", text))


def _variant_idx(job: JobOpportunity) -> int:
    key = f"{(job.company_name or '').strip().lower()}|{job.job_title.strip().lower()}"
    return sum((i + 1) * ord(ch) for i, ch in enumerate(key))


def _nice(name: str) -> str:
    return " ".join(w.capitalize() if len(w) > 3 else w for w in name.split()) if name.isupper() else name


# ------------------------------------------------------------------ recipient & role analysis
_GENERIC_INBOX_LOCAL = {
    "job", "jobs", "career", "careers", "hr", "hrd", "hrga", "talent", "talents", "recruit", "recruiter",
    "recruiting", "recruitment", "hiring", "hire", "apply", "application", "applications", "team", "hello",
    "hi", "hey", "info", "contact", "people", "admin", "office", "engineering", "eng", "tech", "dev", "devs",
    "loker", "lamaran", "karir", "karier", "rekrutmen", "cv", "resume", "join", "work", "support", "mail",
    "email", "noreply", "no", "reply", "founders", "founder", "cto", "ceo", "ops", "peopleops", "staffing",
}


def _recipient_first_name(recipient_email: str | None) -> str | None:
    """Extract a verified human first name from addresses like 'rita@raidsai.ai' (never invent one)."""
    if not recipient_email or "@" not in recipient_email:
        return None
    local, domain = recipient_email.strip().lower().split("@", 1)
    if any(ch.isdigit() for ch in local):
        return None
    parts = [p for p in re.split(r"[._+-]+", local) if p]
    if not parts:
        return None
    first = parts[0]
    if first in _GENERIC_INBOX_LOCAL or any(p in _GENERIC_INBOX_LOCAL for p in parts):
        return None
    if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"} and len(parts) < 2:
        return None
    if not first.isalpha() or not 3 <= len(first) <= 14:
        return None
    return first.capitalize()


_STOP_TERMS = set(
    "senior junior staff principal engineer developer software position role team with and the for "
    "remote hybrid onsite full time part years experience building strong manager project projects "
    "technical leaning integration".split()
)

_VERB_START = re.compile(
    r"^(?:[A-Z][a-z]+ed|Built|Led|Ran|Made|Wrote|Set|Grew|Cut|Drove|Won|Took|Kept|Held|Rebuilt|"
    r"Redesigned|Rewrote|Shipped|Launched|Owned|Ported|Moved|Brought|Taught)\b"
)
_ID_VERB_START = re.compile(
    r"^(?:Membangun|Mengembangkan|Merancang|Mengelola|Memimpin|Membuat|Mengoptimalkan|Mengintegrasikan|"
    r"Meningkatkan|Menangani|Menyusun|Memperbaiki|Memelihara|Mengimplementasikan)\b"
)

_DOMAIN_SIGNALS = (
    "frontend", "backend", "fullstack", "ui", "ux", "redesign", "responsive", "cross-device", "figma",
    "usability", "material design", "wireframes", "prototypes", "gateway", "krakend", "microservices",
    "orchestration", "gemini", "llm", "ai", "chatbot", "cybersecurity", "security", "mfa", "encryption",
    "email", "notification", "notifications", "webhook", "webhooks", "payment", "payments", "payroll",
    "hris", "attendance", "overtime", "insurance", "claim", "claims", "redis", "caching", "queue",
    "queues", "background jobs", "database", "query optimization", "leadership", "leader", "lead",
    "sync", "synchronization", "realtime", "real-time", "map", "gis", "webgis", "postman", "laravel",
    "nestjs", "express", "fastapi", "django", "postgresql", "mysql", "docker", "dashboard", "monitoring",
    "network", "approval", "workflows", "documentation", "clean code", "velocity", "onboarding",
    "architecture", "rest api", "api", "performance",
)

_FOCUS_ANCHORS: dict[str, tuple[str, ...]] = {
    "ai_integration": (
        "gemini", "ai", "llm", "chatbot", "model api", "cybersecurity api", "automated analysis",
        "api gateway", "krakend", "rest api", "fastapi", "python",
    ),
    "frontend": (
        "ui redesign", "cross-device", "responsive", "frontend", "react", "vue", "next.js", "nuxt",
        "tailwind", "page load", "revamp", "figma", "web interface", "webgis",
    ),
    "backend": (
        "krakend", "api gateway", "microservices", "rest api", "express", "nestjs", "node.js",
        "fastapi", "spring boot", "postgresql", "mysql", "redis", "caching", "background jobs",
        "database", "query optimization", "mfa", "encryption", "email service", "data synchronization",
        "queues",
    ),
    "fullstack": (
        "react", "laravel", "nestjs", "express", "node.js", "rest api", "end-to-end", "payroll",
        "hr management", "full-featured", "database", "caching", "responsive web interface",
        "real-time data synchronization", "policy management",
    ),
    "software_engineering": (
        "claim calculation", "overpayment", "80%", "100,000", "40%", "30%", "documentation",
        "clean code", "velocity", "onboarding", "mfa", "encryption", "krakend", "microservices",
        "database optimization", "tens of thousands", "architecture", "rest api",
    ),
}

_FOCUS_PREFERRED_SKILLS: dict[str, tuple[str, ...]] = {
    "ai_integration": ("Python", "FastAPI", "TypeScript", "Node.js", "React", "PostgreSQL", "Docker"),
    "frontend": ("React", "TypeScript", "Next.js", "Vue.js", "Tailwind CSS", "JavaScript", "Figma"),
    "backend": ("Node.js", "NestJS", "Express", "PostgreSQL", "Redis", "TypeScript", "Go", "Python", "FastAPI", "Docker"),
    "fullstack": ("React", "TypeScript", "Node.js", "PostgreSQL", "NestJS", "Express", "Laravel", "Next.js", "Redis", "Docker"),
    "software_engineering": ("TypeScript", "Node.js", "React", "PostgreSQL", "Python", "Go", "Docker"),
}


def detect_role_focus(job: JobOpportunity, analysis: JobAnalysis | None = None) -> str:
    """Classify target posting into: ai_integration | frontend | backend | fullstack | software_engineering."""
    title = (job.job_title or "").lower()
    desc = (job.job_description or "").lower()
    req_text = " ".join(
        list(job.required_skills)
        + (analysis.required_skills if analysis else [])
        + (analysis.responsibilities[:5] if analysis else [])
    ).lower()

    # 1. Explicit AI / LLM integration focus
    if re.search(r"\b(ai|llm|genai|generative\s+ai|chatbot|machine\s+learning|ml)\b", title):
        return "ai_integration"
    if re.search(r"\b(llm|openai|gemini\s+api|langchain|rag|ai[- ]powered\s+chatbot|prompt\s+engineering|ai\s+integration)\b", desc):
        if not re.search(r"\b(front[- ]?end|back[- ]?end)\b", title):
            return "ai_integration"

    # 2. Explicit Frontend / Frontend-Leaning focus
    if re.search(r"\b(front[- ]?end|ui\s+engineer|ui\s+developer|react\s+(?:engineer|developer)|vue\s+(?:engineer|developer)|next\.?js\s+(?:engineer|developer))\b", title):
        return "frontend"

    # 3. Explicit Backend focus
    if re.search(r"\b(back[- ]?end|api\s+(?:engineer|developer)|platform\s+engineer|node\.?js\s+(?:engineer|developer)|golang\s+(?:engineer|developer)|python\s+(?:engineer|developer))\b", title):
        if not re.search(r"\bfull[\s-]?stack\b", title):
            return "backend"

    # 4. Explicit Fullstack focus
    if re.search(r"\bfull[\s-]?stack\b", title):
        return "fullstack"

    # 5. Fallback to skill balance when title is generic ("Software Engineer")
    has_fe = bool(re.search(r"\b(react|vue|next\.?js|nuxt|angular|tailwind|frontend|front-end)\b", req_text))
    has_be = bool(re.search(r"\b(node\.?js|nestjs|express|laravel|fastapi|django|spring|golang|postgresql|redis|microservices|rest\s+api|backend)\b", req_text))
    if re.search(r"\b(software\s+engineer|software\s+developer|systems?\s+engineer)\b", title):
        return "software_engineering"
    if has_fe and has_be:
        return "fullstack"
    if has_fe and not has_be:
        return "frontend"
    if has_be and not has_fe:
        return "backend"
    return "software_engineering"


def _job_weighted_terms(job: JobOpportunity, analysis: JobAnalysis | None = None) -> dict[str, int]:
    """Weighted technical + domain terms from required/preferred skills, title, responsibilities, and description."""
    weights: dict[str, int] = {}

    def add(term: str, w: int) -> None:
        t = term.strip().lower()
        if t and len(t) >= 2:
            weights[t] = max(weights.get(t, 0), w)

    req_skills = list(job.required_skills) + (list(analysis.required_skills) if analysis else [])
    pref_skills = list(job.preferred_skills) + (list(analysis.preferred_skills) if analysis else [])
    for s in req_skills:
        add(s, 7)
    for s in pref_skills:
        add(s, 3)
    for w in re.findall(r"[a-z][a-z0-9+#.-]{1,}", (job.job_title or "").lower()):
        w = w.strip(".-")
        if w and w not in _STOP_TERMS and (len(w) >= 3 or w in {"ui", "ux", "ai", "go"}):
            add(w, 3)

    context_text = "\n".join(
        [job.job_description or ""]
        + (analysis.responsibilities[:8] if analysis else [])
        + (analysis.requirements[:8] if analysis else [])
    ).lower()
    if context_text:
        for sig in _DOMAIN_SIGNALS:
            if _mentions(context_text, sig):
                add(sig, 2)
    return weights


def _select_role_skills(
    job: JobOpportunity,
    profile: CandidateProfile,
    focus: str,
    *,
    matched_skills: list[str] | None = None,
    analysis: JobAnalysis | None = None,
    work_text: str = "",
    limit: int = 4,
) -> list[str]:
    """Pick 3-4 verified concrete CV technologies most relevant to the target role."""
    cv_by_low = {s.lower(): s for s in profile.skills}
    req_set = {s.lower() for s in list(job.required_skills) + (list(analysis.required_skills) if analysis else [])}
    raw_wanted = list(job.required_skills)
    if analysis:
        raw_wanted += list(analysis.required_skills) + list(analysis.preferred_skills)
    raw_wanted += list(matched_skills or []) + list(job.preferred_skills)

    # Concepts/auxiliary tools read awkwardly after "using X, Y, and Z" when concrete tech is available
    non_stack_concepts = {"git", "html", "css", "bootstrap", "postman", "rest api", "microservices", "ci/cd"}
    matched_verified: list[str] = []
    for s in raw_wanted:
        canon = cv_by_low.get(s.lower())
        if canon and canon not in matched_verified:
            matched_verified.append(canon)

    focus_pref = [s.lower() for s in _FOCUS_PREFERRED_SKILLS.get(focus, ())]

    def _skill_rank(skill: str) -> tuple[int, int, int, int]:
        low = skill.lower()
        is_concept = 1 if low in non_stack_concepts else 0
        is_req = 0 if low in req_set else 1
        in_focus = 0 if low in focus_pref else 1
        focus_pos = focus_pref.index(low) if low in focus_pref else 99
        return (is_concept, is_req, in_focus, focus_pos)

    matched_verified.sort(key=_skill_rank)
    concrete = [s for s in matched_verified if s.lower() not in non_stack_concepts]
    chosen = concrete[:limit] if len(concrete) >= 2 else matched_verified[:limit]

    # Top up to at least 3 concrete skills from verified focus-appropriate CV skills if JD had few explicit skills
    if len(chosen) < 3:
        for pref in _FOCUS_PREFERRED_SKILLS.get(focus, ()):
            canon = cv_by_low.get(pref.lower())
            if canon and canon not in chosen:
                chosen.append(canon)
            if len(chosen) >= 3:
                break
    return chosen[:limit]


# ------------------------------------------------------------------ accomplishment selection
_TRAILING_CLAUSE = re.compile(
    r",\s+(?:accompanied\s+by|supported\s+by|resulting\s+in|thereby|in\s+order\s+to|and\s+authored)\b"
)
_SECONDARY_AND = re.compile(r",\s+and\s+(?:executed|implemented|authored|built)\b")


def _safe_comma_cut(text: str, start: int, max_len: int) -> int:
    """Find the last ', ' before max_len that is outside parentheses and not inside a number."""
    depth = 0
    last_good = -1
    for i, ch in enumerate(text):
        if i >= max_len:
            break
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0 and i >= start and i + 1 < len(text) and text[i + 1] == " ":
            last_good = i
    return last_good


def _trim_bullet_preserving_impact(bullet: str, max_len: int = 235) -> str:
    """Keep verified facts and numeric impact intact while preventing overly long multi-clause dumps."""
    b = re.sub(r"\s+", " ", bullet).strip()
    # Always trim noisy trailing filler clauses (", accompanied by ...") when bullet is already substantial
    m_trail = _TRAILING_CLAUSE.search(b, 65)
    if m_trail and len(b) > 150:
        b = b[:m_trail.start()].rstrip(" ,;") + "."
    if len(b) <= max_len:
        return b
    # Keep intact up to 285 chars when a verified metric parenthetical sits cleanly at the very end
    if len(b) <= 285 and re.search(r"\([^)]*(?:\d+%|\$|USD|users|providers)[^)]*\)\.?$", b):
        return b
    # Prefer cutting right after a parenthetical metric (e.g., "(cutting monthly operational costs by 40%)")
    m_metric = re.search(r"\([^)]*(?:\d+%|\$|USD|users|providers)[^)]*\)", b)
    if m_metric and 65 <= m_metric.end() <= max_len:
        tail = b[m_metric.end():]
        if tail.startswith((",", ";", " while", " and")):
            return b[:m_metric.end()].rstrip(" ,;") + "."
    m_sec = _SECONDARY_AND.search(b, 60)
    if m_sec and m_sec.start() <= max_len:
        return b[:m_sec.start()].rstrip(" ,;") + "."
    cut = _safe_comma_cut(b, 70, max_len)
    if cut > 70:
        return b[:cut].rstrip(" ,;") + "."
    return b


def _token_set(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9+#]{3,}", text.lower())
        if w not in {"the", "and", "for", "with", "from", "that", "into", "using", "across", "more"}
    }


def _jaccard(a: str, b: str) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class _HighlightCandidate:
    score: int
    kind: str           # "employment" | "project"
    is_current: bool
    title: str          # role title or project title
    subtitle: str       # employer or project subtitle
    bullet: str         # original bullet
    shaped: str         # trimmed verbatim bullet
    order: int


def _score_candidate(
    bullet: str,
    context: str,
    focus: str,
    terms: dict[str, int],
    *,
    kind: str,
) -> int:
    combined = f"{context} {bullet}"
    score = sum(w for t, w in terms.items() if _mentions(combined, t))
    anchors = _FOCUS_ANCHORS.get(focus, ())
    anchor_hits = sum(1 for a in anchors if _mentions(combined, a))
    score += min(anchor_hits * 3, 9)

    low = combined.lower()
    # Role-specific focus discipline so different role categories pick genuinely distinct work
    if focus == "frontend":
        if any(_mentions(low, k) for k in ("ui redesign", "cross-device", "responsive frontend", "page load", "revamp")):
            score += 14
        elif any(_mentions(low, k) for k in ("responsive", "frontend", "react", "vue", "next.js")):
            score += 7
        elif any(_mentions(low, k) for k in ("wireframes", "prototypes", "usability testing")):
            score -= 2
        elif any(_mentions(low, k) for k in ("payroll", "claim calculation", "krakend", "email service")):
            score -= 6
    elif focus == "backend":
        if any(_mentions(low, k) for k in ("api gateway", "krakend", "microservices", "rest api", "redis", "caching", "background jobs", "email service", "database", "nestjs", "express", "node.js", "fastapi")):
            score += 8
        elif any(_mentions(low, k) for k in ("wireframes", "prototypes", "street view", "ui redesign")):
            score -= 6
    elif focus == "ai_integration":
        if any(_mentions(low, k) for k in ("gemini", "ai", "llm", "chatbot", "model api", "automated analysis")):
            score += 18
        elif any(_mentions(low, k) for k in ("api gateway", "rest api", "krakend", "integration")):
            score += 5
        elif any(_mentions(low, k) for k in ("wireframes", "street view", "payroll")):
            score -= 4
    elif focus == "fullstack":
        has_fe = any(_mentions(low, k) for k in ("react", "vue", "next.js", "frontend", "web interface", "ui"))
        has_be = any(_mentions(low, k) for k in ("laravel", "nestjs", "express", "node.js", "rest api", "database", "redis", "backend"))
        if has_fe and has_be:
            score += 9
        elif has_fe or has_be:
            score += 3
    elif focus == "software_engineering":
        if re.search(r"\b(?:80%|40%|30%|100,000|tens of thousands|>300)\b", low):
            score += 6
        if any(_mentions(low, k) for k in ("architecture", "krakend", "overpayment", "clean code", "documentation", "mfa", "encryption")):
            score += 6

    # Slight preference for professional employment over projects when equally scored (except AI if AI is in project)
    if kind == "employment":
        score += 2
    return score


def _collect_highlights(
    doc: CVDocument | None,
    focus: str,
    terms: dict[str, int],
    *,
    indonesian_bullets: bool = False,
) -> list[_HighlightCandidate]:
    if doc is None:
        return []
    verb_re = _ID_VERB_START if indonesian_bullets else _VERB_START
    out: list[_HighlightCandidate] = []
    order = 0
    for idx, e in enumerate(doc.experience[:4]):
        is_cur = idx == 0 and bool(re.search(r"(?i)present|current|now|sekarang|saat\s+ini", e.dates or ""))
        for b in e.bullets:
            order += 1
            if len(b) > 360 or not verb_re.match(b.strip()):
                continue
            shaped = _trim_bullet_preserving_impact(b)
            sc = _score_candidate(shaped, e.title, focus, terms, kind="employment")
            if sc > 0:
                out.append(_HighlightCandidate(sc, "employment", is_cur, e.title, e.company, b, shaped, order))
    for p in doc.project_entries[:4]:
        for b in p.bullets:
            order += 1
            if len(b) > 360 or not verb_re.match(b.strip()):
                continue
            shaped = _trim_bullet_preserving_impact(b)
            ctx = f"{p.title} {p.company}"
            sc = _score_candidate(shaped, ctx, focus, terms, kind="project")
            if sc > 0:
                out.append(_HighlightCandidate(sc, "project", False, p.title, p.company, b, shaped, order))
    out.sort(key=lambda c: (-c.score, c.order))
    return out


def _pick_top_two(candidates: list[_HighlightCandidate], *, max_combined_chars: int = 390) -> list[_HighlightCandidate]:
    if not candidates:
        return []
    picks = [candidates[0]]
    for cand in candidates[1:]:
        if _jaccard(picks[0].shaped, cand.shaped) >= 0.32:
            continue
        # Avoid repeating the exact same project title twice
        if picks[0].kind == "project" and cand.kind == "project" and picks[0].title.lower() == cand.title.lower():
            continue
        if len(picks[0].shaped) + len(cand.shaped) <= max_combined_chars:
            picks.append(cand)
            break
    return picks


def _render_highlight_en(cand: _HighlightCandidate, *, index: int, variant: int) -> str:
    body = cand.shaped[0].lower() + cand.shaped[1:]
    if cand.kind == "project":
        name = _nice(cand.title)
        label = name if name.lower().endswith(("project", "system", "platform", "app")) else f"{name} project"
        if cand.subtitle and cand.subtitle.lower() not in label.lower():
            lead = f"In a project ({name} – {cand.subtitle}), I " if index == 0 else f"On the {label} ({cand.subtitle}), I "
        else:
            lead = f"On the {label}, I " if index == 0 else f"In addition, on the {label}, I "
    elif index == 0:
        if cand.is_current:
            lead = "In my current role, I " if variant % 2 == 0 else "Most recently, I "
        else:
            lead = "In my professional experience, I " if variant % 2 == 0 else "In recent engineering work, I "
    else:
        has_past_series = bool(re.search(r",\s+(?:implemented|executed|developed|built|integrated)\b", body))
        lead = (
            "Additionally, I "
            if has_past_series
            or not re.match(
                r"^(?:built|led|developed|designed|architected|implemented|integrated|refined|translated|conducted|enhanced|optimized)\b",
                body,
            )
            else "I have also "
        )
    sent = lead + body
    return sent if sent.endswith((".", "!", ")")) else sent + "."


def _build_accomplishments_en(
    doc: CVDocument | None,
    focus: str,
    terms: dict[str, int],
    variant: int,
) -> str:
    candidates = _collect_highlights(doc, focus, terms, indonesian_bullets=False)
    picks = _pick_top_two(candidates)
    if not picks:
        return ""
    return " ".join(_render_highlight_en(c, index=i, variant=variant) for i, c in enumerate(picks))


# ------------------------------------------------------------------ Indonesian accomplishments
_EN_TO_ID_VERB = {
    "led": "memimpin pengembangan",
    "architected": "merancang dan mengimplementasikan",
    "designed": "merancang",
    "integrated": "mengintegrasikan",
    "optimized": "mengoptimalkan",
    "optimised": "mengoptimalkan",
    "enhanced": "meningkatkan performa",
    "refined": "menyempurnakan",
    "transformed": "merancang ulang",
    "translated": "menerjemahkan",
    "conducted": "melaksanakan",
    "built": "membangun",
    "developed": "mengembangkan",
    "implemented": "mengimplementasikan",
}

_IMPACT_PATTERNS_ID: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"reducing\s+financial\s+losses\s+by\s+(\d+%)", re.I), "dengan pengurangan potensi kerugian hingga {0}"),
    (re.compile(r"saving\s+(~?USD\s*[\d,]+/year)", re.I), "dengan efisiensi biaya operasional sekitar {0}"),
    (re.compile(r"cost\s+savings\s+of\s+(~?USD\s*[\d,]+/year)", re.I), "dengan penghematan biaya operasional sekitar {0}"),
    (re.compile(r"cutting\s+monthly\s+operational\s+costs\s+by\s+(\d+%)", re.I), "yang menurunkan biaya operasional bulanan hingga {0}"),
    (re.compile(r"velocity\s+by\s+(\d+%)", re.I), "hingga meningkatkan kecepatan pengembangan tim sebesar {0}"),
    (re.compile(r"processing\s+time\s+by\s+up\s+to\s+(\d+%)", re.I), "yang memangkas waktu proses administratif hingga {0}"),
    (re.compile(r"(100%)\s+cross-device\s+compatibility", re.I), "dengan kompatibilitas lintas perangkat {0}"),
    (re.compile(r"supporting\s+(>\d+\s+providers)", re.I), "yang mendukung {0}"),
]


def _extract_id_impact(text: str) -> str:
    for pat, fmt in _IMPACT_PATTERNS_ID:
        m = pat.search(text)
        if m:
            val = m.group(1).replace("/year", "/tahun").replace("providers", "penyedia layanan")
            return fmt.format(val)
    return ""


def _describe_topic_id(bullet: str, focus: str) -> str:
    low = bullet.lower()
    if "krakend" in low or "api gateway" in low:
        return "API Gateway berbasis KrakenD untuk orkestrasi microservices"
    if "gemini" in low or "cybersecurity api" in low:
        return "integrasi Google Gemini API dan Cybersecurity API untuk analisis otomatis"
    if "email service" in low:
        return "layanan email internal berbasis Express.js beserta MFA dan enkripsi data"
    if "payroll" in low and ("hr management" in low or "prismax" in low):
        return "sistem HR Management dan Payroll terintegrasi"
    if "claim" in low and ("insurance" in low or "overpayment" in low):
        return "logika perhitungan klaim asuransi dan pemrosesan data polis secara real-time"
    if "ui redesign" in low or "cross-device" in low:
        return "perancangan ulang antarmuka web responsif untuk mobile, tablet, dan desktop"
    if "rest api" in low and ("synchronization" in low or "real-time" in low):
        return "REST API backend untuk sinkronisasi data real-time antara server dan antarmuka"
    if "redis" in low and "mfa" in low:
        return "optimasi akses data menggunakan Redis serta pengamanan data sensitif dengan MFA"
    if "database optimization" in low or "background jobs" in low:
        return "optimasi database, caching, dan background jobs pada sistem produksi"
    if "figma" in low and ("frontend" in low or "responsive" in low):
        return "implementasi desain Figma ke kode frontend responsif"
    if "usability testing" in low or "wireframes" in low:
        return "riset pengguna, wireframe, dan prototipe antarmuka sesuai standar Material Design"
    if "network devices" in low:
        return "platform terpusat untuk integrasi dan pemantauan perangkat jaringan lintas vendor"
    return {
        "frontend": "antarmuka aplikasi web responsif",
        "backend": "layanan backend dan REST API produksi",
        "ai_integration": "layanan integrasi API dan fitur berbasis AI",
        "fullstack": "aplikasi web full-stack pada sistem produksi",
    }.get(focus, "sistem dan fitur perangkat lunak pada lingkungan produksi")


def _build_accomplishments_id(
    doc: CVDocument | None,
    focus: str,
    terms: dict[str, int],
    detector: SkillDetector,
) -> str:
    if doc is None:
        return ""
    # 1. Native Indonesian bullets if the CV itself is written in Indonesian
    native = _collect_highlights(doc, focus, terms, indonesian_bullets=True)
    if native:
        picks = _pick_top_two(native)
        parts = []
        for i, c in enumerate(picks):
            body = c.shaped[0].lower() + c.shaped[1:]
            if c.kind == "project":
                lead = f"Pada proyek {_nice(c.title)}, saya "
            elif i == 0:
                lead = "Pada peran saat ini, saya " if c.is_current else "Dalam pengalaman kerja saya, saya "
            else:
                lead = "Selain itu, saya juga "
            sent = lead + body
            parts.append(sent if sent.endswith((".", "!", ")")) else sent + ".")
        return " ".join(parts)

    # 2. English CV applying to Indonesian posting: strictly grounded Bahasa Indonesia synthesis
    en_cands = _collect_highlights(doc, focus, terms, indonesian_bullets=False)
    picks = _pick_top_two(en_cands, max_combined_chars=440)
    if not picks:
        return ""
    parts: list[str] = []
    for i, c in enumerate(picks):
        first_word = (c.bullet.split()[:1] or [""])[0].lower().rstrip(",.;:")
        verb_id = _EN_TO_ID_VERB.get(first_word, "mengembangkan")
        topic = _describe_topic_id(c.bullet, focus)
        techs = [h.name for h in detector.detect(c.shaped)]
        techs = [
            t for t in techs
            if t.lower() not in topic.lower() and t.lower() not in {"postman", "git", "rest api"}
        ][:3]
        impact = _extract_id_impact(c.bullet)
        if c.kind == "project":
            label = f"{_nice(c.title)} ({c.subtitle})" if c.subtitle else _nice(c.title)
            lead = f"Pada proyek {label}, saya {verb_id} {topic}"
        elif i == 0:
            lead = f"Pada peran saat ini, saya {verb_id} {topic}" if c.is_current else f"Dalam pengalaman profesional, saya {verb_id} {topic}"
        else:
            lead = f"Selain itu, saya juga {verb_id} {topic}"
        if techs:
            lead += f" menggunakan {_join(techs, True)}"
        if impact:
            lead += f", {impact}"
        parts.append(lead.rstrip(" ,;") + ".")
    return " ".join(parts)


# ------------------------------------------------------------------ generator class
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

    # ------------------------------------------------------------- deterministic / fallback
    def template_email(
        self,
        job: JobOpportunity,
        profile: CandidateProfile,
        identity: CandidateIdentity,
        *,
        matched_skills: list[str] | None = None,
        doc: CVDocument | None = None,
        language: str | None = None,
        recipient_email: str | None = None,
        analysis: JobAnalysis | None = None,
    ) -> GeneratedEmail:
        """Build a concise, natural, role-tailored application e-mail grounded strictly in CV facts."""
        lang = language or self.language_for(job)
        tpl = self.templates.get(lang) or self.templates["en"]
        indonesian = lang == "id"
        company = (job.company_name or "").strip()
        title = job.job_title.strip()
        headline = profile.current_title or ""
        variant = _variant_idx(job)
        focus = detect_role_focus(job, analysis)
        effective_rcpt = recipient_email or (job.application_emails[0] if job.application_emails else None)
        first_name = _recipient_first_name(effective_rcpt)

        # ---- A. Subject
        if indonesian:
            if identity.name:
                subj_variants = (
                    f"Lamaran – {title} – {identity.name}",
                    f"Lamaran Posisi {title} – {identity.name}",
                )
                subject_line = subj_variants[variant % len(subj_variants)]
            else:
                subject_line = f"Lamaran – {title}"
        else:
            if identity.name:
                subj_variants = (
                    f"Application – {title} – {identity.name}",
                    f"{title} Application – {identity.name}",
                )
                subject_line = subj_variants[variant % len(subj_variants)]
            else:
                subject_line = f"Application – {title}"

        # ---- B. Greeting (never invent a recruiter name)
        if first_name:
            greeting = f"Yth. {first_name}," if indonesian else f"Dear {first_name},"
        elif indonesian:
            greeting = "Yth. Tim Rekrutmen,"
        else:
            if not company:
                greeting = "Dear Hiring Team,"
            else:
                greeting = "Dear Hiring Team," if (variant // 5) % 2 == 0 else "Dear Recruitment Team,"

        # ---- C. Direct Opening
        if indonesian:
            id_openers = (
                f"Saya mengajukan lamaran untuk posisi {title}" + (f" di {company}." if company else "."),
                f"Saya tertarik melamar untuk posisi {title}" + (f" di {company}." if company else "."),
                f"Melalui email ini, saya bermaksud melamar posisi {title}" + (f" di {company}." if company else "."),
            )
            opening_sentence = id_openers[variant % len(id_openers)]
        else:
            en_openers = (
                f"I'm writing to apply for the {title} position" + (f" at {company}." if company else "."),
                f"I'm interested in the {title} opportunity" + (f" at {company}." if company else "."),
                f"I would like to apply for the {title} role" + (f" at {company}." if company else "."),
            )
            opening_sentence = en_openers[variant % len(en_openers)]

        # ---- D. Professional Experience (current title + exact years + 3-4 relevant skills; no forced employer names)
        years_text = ""
        years = profile.experience_years
        if years and years >= 1:
            y = int(years)
            plus = bool(doc and re.search(rf"(?<!\d){y}\s*\+\s*(?:years?|yrs?|tahun)", doc.source_text, re.I))
            more = years - y >= 0.25
            if indonesian:
                years_text = f"lebih dari {y}" if (plus or more) else f"{y}"
            else:
                years_text = f"over {y}" if (plus or more) else f"{y}"

        work_text = " ".join(b for e in ((doc.experience + doc.project_entries) if doc else []) for b in e.bullets)
        role_skills = _select_role_skills(
            job, profile, focus, matched_skills=matched_skills, analysis=analysis, work_text=work_text, limit=4
        )
        entry = doc.experience[0] if doc and doc.experience else None
        is_current = bool(entry and re.search(r"(?i)present|current|now|sekarang|saat\s+ini", entry.dates or ""))
        display_title = (entry.title if (entry and entry.title) else headline) or ""

        focus_phrase_en = {
            "fullstack": "building full-stack web applications",
            "backend": "building backend services and APIs",
            "frontend": "building responsive web interfaces",
            "ai_integration": "developing web backends and API integrations",
            "software_engineering": "building production web applications and backend systems",
        }[focus]
        focus_phrase_id = {
            "fullstack": "pengembangan aplikasi web full-stack",
            "backend": "pengembangan layanan backend dan API",
            "frontend": "pengembangan antarmuka web yang responsif",
            "ai_integration": "pengembangan backend API dan integrasi layanan",
            "software_engineering": "pengembangan aplikasi web dan sistem backend secara end-to-end",
        }[focus]

        skills_tail_en = f" using {_join(role_skills, False)}" if role_skills else ""
        skills_tail_id = f" menggunakan {_join(role_skills, True)}" if role_skills else ""

        if display_title:
            if indonesian:
                lead_id = f"Saat ini saya bekerja sebagai {display_title}" if is_current else f"Saya berpengalaman sebagai {display_title}"
                yr_id = f" dengan pengalaman {years_text} tahun di bidang pengembangan perangkat lunak" if years_text else ""
                role_sentence = f"{lead_id}{yr_id}, yang berfokus pada {focus_phrase_id}{skills_tail_id}."
            else:
                en_role_patterns = (
                    (
                        f"I currently work as {_article(display_title)} {display_title}"
                        if is_current
                        else f"My background is as {_article(display_title)} {display_title}"
                    )
                    + (f" with {years_text} years of experience in software development" if years_text else "")
                    + f", focusing on {focus_phrase_en}{skills_tail_en}.",
                    (
                        f"I currently serve as {_article(display_title)} {display_title}"
                        if is_current
                        else f"I have worked as {_article(display_title)} {display_title}"
                    )
                    + (f", bringing {years_text} years of software development experience" if years_text else "")
                    + f" in {focus_phrase_en}{skills_tail_en}.",
                )
                role_sentence = en_role_patterns[(variant // 3) % len(en_role_patterns)]
        else:
            if indonesian:
                yr_id = f" dengan pengalaman {years_text} tahun" if years_text else ""
                role_sentence = f"Saya seorang pengembang perangkat lunak{yr_id} yang berfokus pada {focus_phrase_id}{skills_tail_id}."
            else:
                yr_en = f" with {years_text} years of experience" if years_text else ""
                role_sentence = f"I am a software engineer{yr_en} focused on {focus_phrase_en}{skills_tail_en}."

        # ---- E. Relevant Accomplishments & Technical Experience (1-2 distinct role-matched items)
        terms = _job_weighted_terms(job, analysis)
        if indonesian:
            achievement = _build_accomplishments_id(doc, focus, terms, self.detector)
        else:
            achievement = _build_accomplishments_en(doc, focus, terms, variant)

        # ---- F. Portfolio & Attachments
        include_figma = focus == "frontend"
        sig_links = _select_signature_links(identity.links, include_figma=include_figma)
        link_labels = [lbl for lbl, _ in sig_links]
        has_portfolio = "Portfolio" in link_labels
        if indonesian:
            if link_labels:
                readable = _join(["portofolio" if l == "Portfolio" else l for l in link_labels], True)
                id_port = (
                    "CV saya lampirkan sebagai bahan pertimbangan. Portofolio dan beberapa proyek yang pernah saya kerjakan juga dapat dilihat melalui tautan di bawah ini."
                    if has_portfolio
                    else f"CV saya lampirkan sebagai bahan pertimbangan, beserta tautan {readable} di bagian bawah.",
                    f"Bersama email ini saya lampirkan CV terbaru. Tautan {readable} saya sertakan di bawah untuk melihat hasil kerja saya lebih lanjut.",
                )
                portfolio_sentence = id_port[(variant // 2) % len(id_port)]
            else:
                portfolio_sentence = "CV terbaru saya lampirkan sebagai bahan pertimbangan Bapak/Ibu."
        else:
            if link_labels:
                readable = _join(["my portfolio" if l == "Portfolio" else l for l in link_labels], False)
                en_port = (
                    "I've attached my CV for your consideration. You can also explore my portfolio and selected projects through the links below."
                    if has_portfolio
                    else f"I've attached my CV for your consideration, along with links to {readable} below.",
                    f"My CV is attached for your review, and you can view {readable} through the links below.",
                    f"I have attached my CV for your review, with links to {readable} included below.",
                )
                portfolio_sentence = en_port[(variant // 11) % len(en_port)]
            else:
                portfolio_sentence = "I've attached my CV for your consideration."

        # ---- G. Confident, Humble Closing
        if indonesian:
            id_closers = (
                ("Saya berharap dapat berdiskusi lebih lanjut mengenai bagaimana pengalaman saya dapat berkontribusi bagi tim Anda.", "Hormat saya,"),
                ("Saya menantikan kesempatan untuk berdiskusi lebih lanjut mengenai lamaran saya.", "Hormat saya,"),
                ("Terima kasih atas waktu dan pertimbangan Bapak/Ibu. Saya siap berdiskusi lebih lanjut jika diperlukan.", "Salam hormat,"),
            )
            closing_sentence, sign_off = id_closers[(variant // 7) % len(id_closers)]
        else:
            en_closers = (
                ("I would welcome the opportunity to discuss how my experience could contribute to your team.", "Best regards,"),
                ("I look forward to the opportunity to discuss my application further.", "Kind regards,"),
                ("I would be glad to discuss how my technical background aligns with your engineering team.", "Best regards,"),
            )
            closing_sentence, sign_off = en_closers[(variant // 7) % len(en_closers)]

        sig_text = signature(identity, include_figma=include_figma)
        values = {
            "subject_line": subject_line,
            "greeting": greeting,
            "opening_sentence": opening_sentence,
            "job_title": title,
            "company": company or ("perusahaan Anda" if indonesian else "your company"),
            "name": identity.name,
            "role_sentence": role_sentence,
            "achievement_sentence": achievement,
            "portfolio_sentence": portfolio_sentence,
            "closing_sentence": closing_sentence,
            "sign_off": sign_off,
            "signature": sig_text,
            # legacy placeholders kept for custom user templates
            "skills_sentence": "",
            "current_title": headline or ("profesional" if indonesian else "professional"),
            "years_phrase": (f" dengan pengalaman {years_text} tahun" if indonesian else
                             f" with {years_text} years of experience") if years_text else "",
            "skills_phrase": "",
            "experience_phrase": achievement,
        }
        try:
            subject = _tidy(str(tpl["subject"]).format(**values))
            body = _tidy(str(tpl["body"]).format(**values))
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigurationError(
                f"E-mail template '{lang}' has an unknown placeholder or brace: {exc}. "
                f"Allowed: {', '.join('{' + k + '}' for k in values)}"
            ) from exc
        subject = re.sub(r"\s*[-–|]\s*$", "", subject)
        notes = [] if job.company_name else ["Company name unknown: a generic greeting is used."]
        return GeneratedEmail(subject=subject, body=body, language=lang, method="template", notes=notes)

    # ------------------------------------------------------------------- AI
    def _ai_candidate_payload(
        self,
        profile: CandidateProfile,
        doc: CVDocument,
        matched_skills: list[str],
        *,
        include_figma: bool = False,
    ) -> dict[str, Any]:
        names = [doc.identity.name] if doc.identity.name else None
        work_text = " ".join(b for e in doc.experience for b in e.bullets)
        in_work = [s for s in matched_skills if _mentions(work_text, s)][:8]
        listed_only = [s for s in matched_skills if s not in in_work][:6]
        projects = [
            {
                "title": p.title,
                "subtitle": p.company,
                "dates": p.dates,
                "highlights": [redact_personal(b, extra_names=names) for b in p.bullets[:3]],
            }
            for p in doc.project_entries[:4]
        ] or [{"title": redact_personal(p, extra_names=names)} for p in doc.projects[:4]]
        link_types = [lbl for lbl, _ in _select_signature_links(doc.identity.links, include_figma=include_figma)]
        return {
            "current_title": doc.experience[0].title if doc.experience else profile.current_title,
            "headline": profile.current_title,
            "experience_years": profile.experience_years,
            "summary": redact_personal(doc.summary, extra_names=names)[:600],
            "matched_skills_in_professional_work": in_work,
            "matched_skills_in_skills_list": listed_only,
            "available_link_types": link_types,
            "professional_experience": [
                {
                    "title": e.title,
                    "dates": e.dates,
                    "is_current": bool(idx == 0 and re.search(r"(?i)present|current|now|sekarang", e.dates or "")),
                    "highlights": [redact_personal(b, extra_names=names) for b in e.bullets[:4]],
                }
                for idx, e in enumerate(doc.experience[:4])
            ],
            "projects": projects,
        }

    def _effective_language(self, analysis: JobAnalysis) -> str | None:
        """Honor configured email_language ('en'|'id'); use analysis.language only when 'auto'."""
        if self.default_language in self.templates:
            return self.default_language
        return analysis.language if analysis.language in self.templates else None

    async def ai_email(
        self,
        job: JobOpportunity,
        analysis: JobAnalysis,
        profile: CandidateProfile,
        doc: CVDocument,
        matched_skills: list[str],
        *,
        recipient_email: str | None = None,
    ) -> GeneratedEmail:
        fallback = self.template_email(
            job,
            profile,
            doc.identity,
            matched_skills=matched_skills,
            doc=doc,
            language=self._effective_language(analysis),
            recipient_email=recipient_email,
            analysis=analysis,
        )
        if self.ai is None or not self.ai.available:
            fallback.notes.append("AI unavailable: template e-mail used.")
            return fallback
        lang = fallback.language
        focus = detect_role_focus(job, analysis)
        effective_rcpt = recipient_email or (job.application_emails[0] if job.application_emails else None)
        job_info = {
            "job_title": job.job_title,
            "company": job.company_name or "",
            "role_category": focus,
            "seniority": job.seniority or "",
            "location": job.location or "",
            "work_arrangement": job.work_arrangement or "",
            "recruiter_first_name": _recipient_first_name(effective_rcpt),
            "required_skills": analysis.required_skills[:10] or job.required_skills[:10],
            "preferred_skills": analysis.preferred_skills[:8] or job.preferred_skills[:8],
            "requirements": analysis.requirements[:8],
            "responsibilities": analysis.responsibilities[:6],
            "description_excerpt": redact_personal((job.job_description or "")[:900]),
        }
        cand = self._ai_candidate_payload(profile, doc, matched_skills, include_figma=(focus == "frontend"))
        result = await self.ai.ask_json("application_email", application_email_prompt(job_info, cand, lang), cache_days=7)
        if not isinstance(result, dict):
            fallback.notes.append("AI e-mail unavailable: template e-mail used.")
            return fallback
        subject = re.sub(r"\s+", " ", str(result.get("subject") or "")).strip()[:150]
        body = _tidy(str(result.get("body") or ""))
        problems: list[str] = []
        years = {str(int(profile.experience_years))} if profile.experience_years else set()
        checked = body + " " + subject
        for name in (job.job_title, job.company_name or ""):
            if name.strip():
                checked = re.sub(re.escape(name.strip()), " ", checked, flags=re.I)
        # Allow referencing verified link platforms (e.g. "GitHub") without triggering a false "Git" skill error
        allowed_ctx = " ".join(lbl for lbl, _ in _select_signature_links(doc.identity.links, include_figma=(focus == "frontend")))
        problems += check_free_text(checked, doc.source_text, self.detector, allowed_text=allowed_ctx, allowed_numbers=years)
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
        if not re.search(
            r"(?im)^(kind regards|best regards|regards|sincerely|best|thanks|hormat saya|salam|salam hormat|salam hangat|terima kasih)[,.]?\s*$",
            body,
        ):
            body = f"{body}\n\n{closing}"
        body = f"{body}\n{signature(doc.identity, include_figma=(focus == 'frontend'))}"
        if job.job_title.lower() not in subject.lower():
            subject = fallback.subject
        elif doc.identity.name and doc.identity.name.lower() not in subject.lower():
            subject = f"{subject} – {doc.identity.name}"
        return GeneratedEmail(subject=subject[:150], body=body, language=lang, method="ai", notes=[])

    # ------------------------------------------------------------ web cover note
    async def cover_note(
        self,
        job: JobOpportunity,
        analysis: JobAnalysis,
        profile: CandidateProfile,
        doc: CVDocument,
        matched_skills: list[str],
        *,
        use_ai: bool = True,
    ) -> GeneratedEmail:
        """Concise, paste-ready cover note for Web/ATS application forms (channel='web')."""
        base = self.template_email(
            job,
            profile,
            doc.identity,
            matched_skills=matched_skills,
            doc=doc,
            language=self._effective_language(analysis),
            analysis=analysis,
        )
        lang = base.language
        indonesian = lang == "id"
        # Convert the e-mail into a clean form-ready cover note (no greeting, attachment line, or signature).
        lines = [l for l in base.body.splitlines() if not l.strip().lower().startswith(("dear ", "hi ", "halo ", "yth."))]
        trimmed_lines: list[str] = []
        for l in lines:
            if re.match(
                r"(?i)^(best regards|kind regards|best|thanks|hormat saya|salam|salam hormat|salam hangat|terima kasih|sincerely|regards)[,.]?\s*$",
                l.strip(),
            ):
                break
            if re.search(r"(?i)(cv is attached|attached my cv|cv saya lampirkan|lampirkan cv|melampirkan cv|please find my cv attached)", l):
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
                    rf"(the\s+{re.escape(job.job_title.strip())}\s+(?:position|role|opportunity))\.",
                    rf"\1 at {company}.",
                    fallback_body,
                    count=1,
                )
        if not fallback_body:
            fallback_body = base.body
        subject = f"Cover Note: {job.job_title.strip()}" + (f" – {job.company_name.strip()}" if job.company_name else "")
        fallback = GeneratedEmail(
            subject=subject[:150],
            body=fallback_body,
            language=lang,
            method="template",
            notes=list(base.notes),
        )
        if not use_ai or self.ai is None or not self.ai.available:
            if use_ai and (self.ai is None or not self.ai.available):
                fallback.notes.append("AI unavailable: template cover note used.")
            return fallback
        focus = detect_role_focus(job, analysis)
        job_info = {
            "job_title": job.job_title,
            "company": job.company_name or "",
            "role_category": focus,
            "required_skills": analysis.required_skills[:10] or job.required_skills[:10],
            "preferred_skills": analysis.preferred_skills[:8] or job.preferred_skills[:8],
            "requirements": analysis.requirements[:8],
            "responsibilities": analysis.responsibilities[:6],
        }
        cand = self._ai_candidate_payload(profile, doc, matched_skills, include_figma=(focus == "frontend"))
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
