"""Interactive search questionnaire.

``default_answers`` and ``build_search_config`` are pure functions (unit-tested);
``ask_questions`` drives Questionary prompts where pressing Enter keeps the default.
"""

from __future__ import annotations

import re
from typing import Any

from src.core.configuration import Settings
from src.core.models import SearchConfig, SourceType
from src.cv.models import CandidateProfile
from src.extraction.content_parser import SENIORITY_ORDER

GEOGRAPHY = {
    "indonesia": "Indonesia",
    "worldwide": "Worldwide",
    "indonesia_worldwide": "Indonesia + Worldwide",
    "selected": "Selected countries",
}
EMPLOYMENT = {"full_time": "Full-time", "contract": "Contract", "part_time": "Part-time", "internship": "Internship",
              "freelance": "Freelance", "all": "All employment types"}
POSITION_MODES = {
    "ai_recommended": "AI-recommended positions based on CV",
    "current_title": "Use current job title",
    "custom": "Specify custom positions",
    "multiple": "Choose multiple target positions",
}
ARRANGEMENTS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "remote_hybrid": "Remote + Hybrid",
                "all": "All arrangements"}
WINDOWS = {"1": "Last 24 hours", "3": "Last 3 days", "7": "Last 7 days", "14": "Last 14 days", "30": "Last 30 days",
           "all": "All available dates"}
SENIORITY_MODES = {"ai_recommended": "AI recommendation based on CV", "entry": "Entry-level", "junior": "Junior",
                   "mid": "Mid-level", "senior": "Senior", "lead": "Lead / Supervisor", "all": "All relevant levels"}
SOURCES = {
    SourceType.JOB_PORTAL.value: "Job Portals",
    SourceType.COMPANY_CAREERS.value: "Official Company Career Pages",
    SourceType.ATS.value: "ATS Platforms",
    SourceType.SOCIAL_MEDIA.value: "Public Social Media Posts",
    SourceType.COMMUNITY.value: "Public Online Communities",
    SourceType.SEARCH_ENGINE.value: "Search Engines",
    SourceType.OTHER.value: "Other accessible public sources",
}
LANGUAGES = {"en": "English", "id": "Indonesian", "both": "Both (English + Indonesian)", "additional": "Add other languages"}
CURRENCIES = ["IDR", "USD", "SGD", "MYR", "EUR", "GBP", "AUD", "JPY", "AED", "CAD"]


def default_answers(settings: Settings, saved: dict[str, Any] | None, profile: CandidateProfile) -> dict[str, Any]:
    """Defaults: saved preferences > settings.yaml > built-ins."""
    saved = dict(saved or {})
    window = settings.get("search.default_publication_window_days", 7)
    langs = settings.get("search.default_languages", ["en", "id"]) or ["en", "id"]
    defaults: dict[str, Any] = {
        "geography": settings.get("search.default_geography", "indonesia_worldwide"),
        "countries": list(settings.get("search.default_countries", ["Indonesia"]) or ["Indonesia"]),
        "employment_types": list(settings.get("search.default_employment_types", ["full_time"]) or ["full_time"]),
        "position_mode": "ai_recommended",
        "custom_positions": [],
        "arrangement": "all",
        "window": str(window) if window else "all",
        "min_salary": None,
        "salary_currency": "IDR",
        "include_undisclosed": bool(settings.get("search.include_undisclosed_salary", True)),
        "seniority_mode": "ai_recommended",
        "sources": [s.value for s in SourceType],
        "languages": "both" if set(langs) >= {"en", "id"} else langs[0],
        "extra_languages": [],
        "additional": "",
        "include_previous": bool(settings.get("search.include_previous_jobs", False)),
    }
    arr = settings.get("search.default_work_arrangements")
    if arr:
        defaults["arrangement"] = _arrangement_key(arr)
    for k in defaults:
        if k in saved and saved[k] is not None:
            defaults[k] = saved[k]
    return defaults


def _arrangement_key(values: list[str]) -> str:
    s = set(values)
    if s == {"remote"}:
        return "remote"
    if s == {"hybrid"}:
        return "hybrid"
    if s == {"onsite"}:
        return "onsite"
    if s == {"remote", "hybrid"}:
        return "remote_hybrid"
    return "all"


def recommended_seniority(profile: CandidateProfile) -> list[str]:
    est = profile.seniority_estimate if profile.seniority_estimate in SENIORITY_ORDER else None
    if not est:
        return []
    i = SENIORITY_ORDER.index(est)
    return SENIORITY_ORDER[max(0, i - 1): i + 2]


def resolve_positions(answers: dict[str, Any], profile: CandidateProfile) -> list[str]:
    mode = answers.get("position_mode", "ai_recommended")
    if mode == "current_title" and profile.current_title:
        return [profile.current_title]
    if mode in ("custom", "multiple") and answers.get("custom_positions"):
        return [p for p in answers["custom_positions"] if p.strip()]
    return list(profile.target_positions[:6]) or ([profile.current_title] if profile.current_title else [])


def build_search_config(answers: dict[str, Any], settings: Settings, profile: CandidateProfile) -> SearchConfig:
    geo = answers.get("geography", "indonesia_worldwide")
    if geo == "indonesia":
        countries, worldwide = ["Indonesia"], False
    elif geo == "worldwide":
        countries, worldwide = [], True
    elif geo == "selected":
        countries, worldwide = [c for c in answers.get("countries", []) if c] or ["Indonesia"], False
    else:
        countries, worldwide = ["Indonesia"], True
    emp = answers.get("employment_types") or ["full_time"]
    if "all" in emp:
        emp = ["full_time", "contract", "part_time", "internship", "freelance"]
    arr = {
        "remote": ["remote"], "hybrid": ["hybrid"], "onsite": ["onsite"], "remote_hybrid": ["remote", "hybrid"],
    }.get(answers.get("arrangement", "all"), ["remote", "hybrid", "onsite"])
    w = str(answers.get("window", "7"))
    window = None if w == "all" else int(w)
    lang = answers.get("languages", "both")
    langs = {"en": ["en"], "id": ["id"], "both": ["en", "id"]}.get(lang, ["en", "id"]) if isinstance(lang, str) else list(lang)
    langs += [l for l in answers.get("extra_languages", []) if l and l not in langs]
    smode = answers.get("seniority_mode", "ai_recommended")
    if smode == "ai_recommended":
        levels = recommended_seniority(profile)
    elif smode == "all":
        levels = []
    else:
        levels = [smode]
    min_salary = answers.get("min_salary")
    try:
        min_salary = float(min_salary) if min_salary not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        min_salary = None
    return SearchConfig(
        geography_mode=geo,
        countries=countries,
        include_worldwide=worldwide,
        employment_types=emp,
        position_mode=answers.get("position_mode", "ai_recommended"),
        target_positions=resolve_positions(answers, profile),
        work_arrangements=arr,
        publication_window_days=window,
        min_salary=min_salary,
        salary_currency=(answers.get("salary_currency") or None) if min_salary else answers.get("salary_currency"),
        include_undisclosed_salary=bool(answers.get("include_undisclosed", True)),
        seniority_mode="custom" if smode not in ("ai_recommended", "all") else smode,
        seniority_levels=levels,
        source_categories=list(answers.get("sources") or [s.value for s in SourceType]),
        languages=langs,
        additional_preferences=str(answers.get("additional") or "").strip()[:1000],
        include_previous_jobs=bool(answers.get("include_previous", False)),
    )


def parse_list(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]


# ------------------------------------------------------------------ prompts

class Cancelled(Exception):
    """User aborted the questionnaire (Ctrl+C / Esc)."""


def _ans(value: Any) -> Any:
    if value is None:
        raise Cancelled()
    return value


def ask_questions(settings: Settings, defaults: dict[str, Any], profile: CandidateProfile) -> dict[str, Any]:
    import questionary
    from questionary import Choice

    a = dict(defaults)
    country_names = list(settings.country_table().keys())

    # 1. Geography
    a["geography"] = _ans(questionary.select(
        "1/10  Search geography:", choices=[Choice(v, k) for k, v in GEOGRAPHY.items()], default=GEOGRAPHY_choice(a)
    ).ask())
    if a["geography"] == "selected":
        chosen = _ans(questionary.checkbox(
            "      Select countries (space to toggle, Enter to confirm):",
            choices=[Choice(c, c, checked=c in a.get("countries", [])) for c in country_names],
        ).ask())
        a["countries"] = chosen or a.get("countries") or ["Indonesia"]

    # 2. Employment type
    chosen = _ans(questionary.checkbox(
        "2/10  Employment type (Enter keeps the checked defaults):",
        choices=[Choice(v, k, checked=k in a["employment_types"]) for k, v in EMPLOYMENT.items()],
    ).ask())
    a["employment_types"] = chosen or a["employment_types"]

    # 3. Positions
    rec = ", ".join(profile.target_positions[:6]) or "(none detected)"
    a["position_mode"] = _ans(questionary.select(
        f"3/10  Target positions  [CV-based suggestions: {rec}]:",
        choices=[Choice(v, k) for k, v in POSITION_MODES.items()], default=_choice(POSITION_MODES, a["position_mode"]),
    ).ask())
    if a["position_mode"] == "custom":
        text = _ans(questionary.text("      Enter positions (comma separated):",
                                     default=", ".join(a.get("custom_positions") or [])).ask())
        a["custom_positions"] = parse_list(text)
    elif a["position_mode"] == "multiple":
        options = list(dict.fromkeys(profile.target_positions + profile.adjacent_positions + a.get("custom_positions", [])))
        picked = _ans(questionary.checkbox(
            "      Select positions:", choices=[Choice(p, p, checked=p in profile.target_positions[:4]) for p in options]
        ).ask())
        extra = _ans(questionary.text("      Additional positions (optional, comma separated):", default="").ask())
        a["custom_positions"] = list(dict.fromkeys((picked or profile.target_positions[:4]) + parse_list(extra)))

    # 4. Arrangement
    a["arrangement"] = _ans(questionary.select(
        "4/10  Work arrangement:", choices=[Choice(v, k) for k, v in ARRANGEMENTS.items()],
        default=_choice(ARRANGEMENTS, a["arrangement"])).ask())

    # 5. Publication date
    a["window"] = _ans(questionary.select(
        "5/10  Job publication date:", choices=[Choice(v, k) for k, v in WINDOWS.items()],
        default=_choice(WINDOWS, str(a["window"]))).ask())

    # 6. Salary
    sal = _ans(questionary.text("6/10  Minimum salary (number, Enter = no minimum):",
                                default=str(int(a["min_salary"])) if a.get("min_salary") else "",
                                validate=lambda t: t.strip() == "" or t.replace(".", "", 1).replace(",", "").isdigit()
                                or "Enter a number or leave empty").ask())
    a["min_salary"] = float(sal.replace(",", "")) if sal.strip() else None
    if a["min_salary"]:
        a["salary_currency"] = _ans(questionary.select("      Preferred currency:", choices=CURRENCIES,
                                                       default=a.get("salary_currency") or "IDR").ask())
    a["include_undisclosed"] = _ans(questionary.confirm("      Include opportunities with undisclosed salary?",
                                                        default=bool(a["include_undisclosed"])).ask())

    # 7. Seniority
    est = profile.seniority_estimate or "unknown"
    a["seniority_mode"] = _ans(questionary.select(
        f"7/10  Experience level  [CV estimate: {est}]:", choices=[Choice(v, k) for k, v in SENIORITY_MODES.items()],
        default=_choice(SENIORITY_MODES, a["seniority_mode"])).ask())

    # 8. Sources
    chosen = _ans(questionary.checkbox(
        "8/10  Search sources (Enter keeps the checked defaults):",
        choices=[Choice(v, k, checked=k in a["sources"]) for k, v in SOURCES.items()]).ask())
    a["sources"] = chosen or a["sources"]

    # 9. Language
    lang = a["languages"] if isinstance(a["languages"], str) else "both"
    a["languages"] = _ans(questionary.select(
        "9/10  Search language:", choices=[Choice(v, k) for k, v in LANGUAGES.items()],
        default=_choice(LANGUAGES, lang)).ask())
    if a["languages"] == "additional":
        extra = _ans(questionary.text("      Extra language codes (e.g. ms, ja, de):",
                                      default=", ".join(a.get("extra_languages", []))).ask())
        a["extra_languages"] = parse_list(extra)
        a["languages"] = "both"

    # 10. Additional preferences
    a["additional"] = _ans(questionary.text(
        "10/10 Additional preferences (optional, e.g. 'Prefer startups. Avoid banking companies.'):",
        default=a.get("additional") or "").ask())
    a["include_previous"] = _ans(questionary.confirm(
        "      Include still-open jobs found in previous searches?", default=bool(a.get("include_previous"))).ask())
    return a


def _choice(mapping: dict[str, str], key: str) -> str | None:
    return mapping.get(key) and key


def GEOGRAPHY_choice(a: dict[str, Any]) -> str:  # noqa: N802 - small helper
    return a["geography"] if a.get("geography") in GEOGRAPHY else "indonesia_worldwide"
