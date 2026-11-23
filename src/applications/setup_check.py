"""Configuration validation and the user-facing setup checklist (``python main.py --setup-check``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import re

from src.applications.contacts import validate_address
from src.applications.email_service import transport_kind
from src.applications.gmail_client import GmailAuth
from src.applications.sending_limits import SendingLimits
from src.core.configuration import Settings
from src.core.exceptions import ConfigurationError
from src.cv.parser import list_cv_files

VALID_CATEGORIES = {"HIGHLY RELEVANT", "RELEVANT", "POTENTIALLY RELEVANT", "NEEDS MANUAL REVIEW", "NOT RELEVANT"}
VALID_STATUSES = {"VERIFIED_OPEN", "LIKELY_OPEN", "UNVERIFIED", "NEEDS_REVIEW", "CLOSED", "INACCESSIBLE", "POSSIBLE_SCAM"}


def _num(errors: list[str], cfg: dict[str, Any], key: str, path: str, lo: float, hi: float) -> None:
    if key not in cfg or cfg[key] is None:
        return
    v = cfg[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        errors.append(f"{path}.{key} must be a number between {lo:g} and {hi:g} (got {v!r})")


def _list(errors: list[str], cfg: dict[str, Any], key: str, path: str) -> list[Any]:
    v = cfg.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        errors.append(f"{path}.{key} must be a list (got {v!r})")
        return []
    return v


def validate_applications_settings(settings: Settings) -> None:
    """Raise ConfigurationError with every problem in the ``applications`` section."""
    errors: list[str] = []
    app = settings.get("applications", {}) or {}
    if not isinstance(app, dict):
        raise ConfigurationError("applications must be a mapping in config/settings.yaml")
    el = app.get("eligibility") or {}
    _num(errors, el, "min_relevance_score", "applications.eligibility", 0, 100)
    _num(errors, el, "max_posting_age_days", "applications.eligibility", 1, 3650)
    bad = [c for c in _list(errors, el, "allowed_relevance_categories", "applications.eligibility")
           if str(c).upper() not in VALID_CATEGORIES]
    if bad:
        errors.append(f"applications.eligibility.allowed_relevance_categories: unknown {bad}; use {sorted(VALID_CATEGORIES)}")
    bad = [s for s in _list(errors, el, "excluded_verification_statuses", "applications.eligibility")
           if s not in VALID_STATUSES]
    if bad:
        errors.append(f"applications.eligibility.excluded_verification_statuses: unknown {bad}")
    for key in ("title_include_keywords", "title_exclude_keywords", "exclude_companies", "allowed_employment_types"):
        _list(errors, el, key, "applications.eligibility")
    ai = app.get("ai") or {}
    _num(errors, ai, "min_match_score", "applications.ai", 0, 100)
    _num(errors, ai, "max_calls_per_run", "applications.ai", 0, 1000)
    _num(errors, ai, "max_jobs_per_run", "applications.ai", 1, 500)
    _num(errors, ai, "max_cv_pages", "applications.ai", 1, 10)
    weights = ai.get("match_weights")
    if weights is not None:
        if not isinstance(weights, dict) or not all(isinstance(v, (int, float)) and v >= 0 for v in weights.values()) \
                or sum(weights.values()) <= 0:
            errors.append("applications.ai.match_weights must map skills/title/experience/keywords to "
                          "non-negative numbers with a positive sum")
    _num(errors, app, "max_jobs_per_run", "applications", 1, 1000)
    lang = str(app.get("email_language", "auto"))
    if lang not in ("auto", "en", "id"):
        errors.append("applications.email_language must be auto, en or id")
    s = app.get("sending") or {}
    if str(s.get("mode", "manual")) not in ("manual", "scheduled", "automatic"):
        errors.append("applications.sending.mode must be manual, scheduled or automatic")
    if str(s.get("transport", "auto")) not in ("auto", "smtp", "gmail_api"):
        errors.append("applications.sending.transport must be auto, smtp or gmail_api")
    if s.get("smtp_port", 587) not in (465, 587):
        errors.append("applications.sending.smtp_port must be 587 (STARTTLS) or 465 (TLS)")
    _num(errors, s, "automatic_min_match_score", "applications.sending", 0, 100)
    _num(errors, s, "max_attachment_mb", "applications.sending", 0.1, 25)
    _num(errors, s, "max_send_attempts", "applications.sending", 1, 5)
    _num(errors, s, "max_wait_seconds", "applications.sending", 0, 7200)
    try:
        SendingLimits.from_settings(settings)
    except (ConfigurationError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    if errors:
        raise ConfigurationError("Invalid application settings:\n  - " + "\n  - ".join(errors))


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required_for: str = ""   # "all" | "live" | "ai"


def setup_checklist(settings: Settings, jobs_with_email: int | None = None) -> list[CheckItem]:
    items: list[CheckItem] = []
    try:
        validate_applications_settings(settings)
        items.append(CheckItem("Application settings", True, "config/settings.yaml applications: section is valid",
                               required_for="all"))
    except ConfigurationError as exc:
        items.append(CheckItem("Application settings", False, str(exc), "Fix the listed values in config/settings.yaml",
                               "all"))
    cvs = list_cv_files(settings.cv_directory)
    pdfs = [c for c in cvs if c.suffix.lower() == ".pdf"]
    items.append(CheckItem("CV in cv/ folder", bool(cvs), ", ".join(c.name for c in cvs) or "no CV found",
                           f"Copy your CV into {settings.cv_directory}", "all"))
    items.append(CheckItem("PDF CV (Basic mode attachment)", bool(pdfs), ", ".join(p.name for p in pdfs) or "no PDF",
                           "Save/export your CV as PDF into the cv/ folder", "all"))
    if jobs_with_email is not None:
        items.append(CheckItem("Scraped jobs with an application e-mail", jobs_with_email > 0,
                               f"{jobs_with_email} in the latest search",
                               "Run a search first: python main.py (a SEARCH_API_KEY finds more e-mail postings)", "all"))
    dry = bool(settings.get("applications.sending.dry_run", True))
    items.append(CheckItem("Sending mode", True, "DRY RUN (nothing is sent; .eml files in data/outbox)" if dry else
                           "LIVE (sends through Gmail)", "Set applications.sending.dry_run: false to send for real",
                           "live"))
    sender = os.environ.get("GMAIL_SENDER_ADDRESS", "").strip()
    items.append(CheckItem("GMAIL_SENDER_ADDRESS in .env", bool(sender) and validate_address(sender)[0],
                           sender or "not set", "Add GMAIL_SENDER_ADDRESS=you@gmail.com to .env", "live"))
    kind = transport_kind(settings)
    items.append(CheckItem("Gmail transport", True, "SMTP with App Password" if kind == "smtp" else
                           "Gmail API with OAuth (gmail.send)", "applications.sending.transport in settings.yaml",
                           "live"))
    if kind == "smtp":
        pw = re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD", ""))
        items.append(CheckItem("GMAIL_APP_PASSWORD in .env", len(pw) == 16,
                               "set (value hidden)" if len(pw) == 16 else ("not set" if not pw else
                                                                           f"wrong length ({len(pw)}, expected 16)"),
                               "Create one at https://myaccount.google.com/apppasswords (needs 2-Step Verification) "
                               "and put it in .env yourself - never share it", "live"))
        return items + _ai_and_identity(settings)
    auth = GmailAuth.from_settings(settings)
    items.append(CheckItem("Gmail OAuth client file", auth.client_secret_file.exists(), str(auth.client_secret_file),
                           "Google Cloud Console: enable Gmail API, create a 'Desktop app' OAuth client, "
                           "download the JSON to this path (or use an App Password: GMAIL_APP_PASSWORD)", "live"))
    items.append(CheckItem("Gmail authorisation token", auth.has_token(), str(auth.token_file),
                           "Run: python main.py --gmail-auth (again every 7 days while the app is in 'Testing')",
                           "live"))
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
        libs = True
    except ImportError:
        libs = False
    items.append(CheckItem("Google auth libraries", libs, "installed" if libs else "missing",
                           "pip install -r requirements.txt", "live"))
    return items + _ai_and_identity(settings)


def _ai_and_identity(settings: Settings) -> list[CheckItem]:
    items: list[CheckItem] = []
    ai_ok = settings.credentials.has_ai
    items.append(CheckItem("GEMINI_API_KEY (AI mode)", ai_ok, "set" if ai_ok else "not set - AI mode only reorders the CV",
                           "Add GEMINI_API_KEY to .env (https://aistudio.google.com/apikey)", "ai"))
    name = os.environ.get("APPLICANT_NAME", "").strip()
    items.append(CheckItem("Applicant name/contact", True, name or "taken from the CV header",
                           "Optional: APPLICANT_NAME / APPLICANT_PHONE / APPLICANT_LINKS in .env", "all"))
    return items
