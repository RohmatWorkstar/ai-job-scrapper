"""Search query generation from the candidate profile and the search configuration.

Produces a :class:`QueryPlan`: search-engine queries per source category (each
within a share of the paid query budget) plus short keyword lists for API feeds.
Queries are generated dynamically from target roles, skills, countries,
arrangement, employment type, language and hiring phrases - never one generic query.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from src.core.configuration import Settings
from src.core.models import QuerySpec, SearchConfig, SourceType
from src.cv.models import CandidateProfile

# Share of the paid search budget per category (renormalised over enabled categories).
CATEGORY_SHARE = {
    SourceType.JOB_PORTAL: 0.28,
    SourceType.SOCIAL_MEDIA: 0.24,
    SourceType.SEARCH_ENGINE: 0.14,
    SourceType.ATS: 0.14,
    SourceType.COMMUNITY: 0.10,
    SourceType.COMPANY_CAREERS: 0.10,
}
EMPLOYMENT_WORDS = {"contract": "contract", "part_time": "part-time", "internship": "internship", "freelance": "freelance"}
PREFERENCE_POSITIVE_RE = re.compile(r"(?i)^\s*(?:prefer(?:ably)?|include|prioriti[sz]e|focus on|target)\s+(.+)$")
PREFERENCE_NEGATIVE_RE = re.compile(r"(?i)^\s*(?:avoid|exclude|no|not|skip|hindari|tanpa)\s+(.+)$")


@dataclass
class Preferences:
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    raw: str = ""


_STOP = {"jobs", "job", "companies", "company", "opportunities", "opportunity", "positions", "roles", "that", "are",
         "the", "a", "an", "and", "or", "with", "in", "for", "to", "of"}


def _clean_term(term: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9+#.\-]+", term.lower()) if w not in _STOP]
    return " ".join(words[:4])


def parse_preferences(text: str) -> Preferences:
    prefs = Preferences(raw=text or "")
    for chunk in re.split(r"[.;\n]+", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        neg = PREFERENCE_NEGATIVE_RE.match(chunk)
        pos = PREFERENCE_POSITIVE_RE.match(chunk)
        if neg:
            t = _clean_term(neg.group(1))
            if t:
                prefs.negative.append(t)
        elif pos:
            t = _clean_term(pos.group(1))
            if t:
                prefs.positive.append(t)
    return prefs


@dataclass
class QueryPlan:
    queries: dict[SourceType, list[QuerySpec]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)       # short terms for API feeds (e.g. "node.js")
    role_terms: list[str] = field(default_factory=list)     # role titles
    preferences: Preferences = field(default_factory=Preferences)

    def for_source(self, category: SourceType, source_name: str | None = None) -> list[QuerySpec]:
        return list(self.queries.get(category, []))

    def all(self) -> list[QuerySpec]:
        return [q for qs in self.queries.values() for q in qs]

    def summary(self) -> dict[str, int]:
        return {k.value: len(v) for k, v in self.queries.items()}


class QueryGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profiles = settings.profiles
        self.countries = settings.country_table()

    # --------------------------------------------------------------- inputs
    def roles(self, profile: CandidateProfile, config: SearchConfig, limit: int = 4) -> list[str]:
        roles = config.target_positions or profile.target_positions or ([profile.current_title] if profile.current_title else [])
        seen: list[str] = []
        for r in roles:
            r = re.sub(r"\s+", " ", r).strip()
            if r and r.lower() not in {s.lower() for s in seen}:
                seen.append(r)
        return seen[:limit] or ["Software Engineer"]

    def keywords(self, profile: CandidateProfile, config: SearchConfig, limit: int = 8) -> list[str]:
        fams = self.profiles.get("role_families") or {}
        kws: list[str] = []
        for fam in profile.role_families:
            if fam == "software_engineering":
                continue
            k = (fams.get(fam, {}).get("keywords") or [None])[0]
            if k and k not in kws:
                kws.append(k)
        for r in config.target_positions:
            t = re.sub(r"(?i)\b(developer|engineer|programmer)\b", "", r).strip().lower()
            if t and t not in kws and len(t) > 2:
                kws.append(t)
        core = [s for s in profile.frameworks + profile.programming_languages
                if s not in {"HTML", "CSS", "SQL", "Bash"}]
        for s in core:
            if s.lower() not in kws:
                kws.append(s.lower())
        return kws[:limit] or ["software engineer"]

    def country_targets(self, config: SearchConfig) -> list[tuple[str | None, str | None]]:
        """(country name, ISO code) pairs; (None, None) represents remote/worldwide."""
        out: list[tuple[str | None, str | None]] = []
        for c in config.countries:
            out.append((c, (self.countries.get(c) or {}).get("code")))
        if config.include_worldwide:
            out.append((None, None))
            for c in self.settings.get("search.worldwide_countries", [])[:4]:
                if c not in config.countries:
                    out.append((c, (self.countries.get(c) or {}).get("code")))
        return out or [(None, None)]

    # ------------------------------------------------------------ builders
    def _arrangement_word(self, config: SearchConfig) -> str:
        arr = set(config.work_arrangements)
        if arr == {"remote"}:
            return "remote"
        if arr == {"hybrid"}:
            return "hybrid"
        if arr == {"remote", "hybrid"}:
            return "remote"
        return ""

    def _employment_word(self, config: SearchConfig) -> str:
        types = [t for t in config.employment_types if t != "full_time"]
        if len(config.employment_types) == 1 and types:
            return EMPLOYMENT_WORDS.get(types[0], "")
        return ""

    def _q(self, text: str, cat: SourceType, purpose: str, config: SearchConfig, *, lang: str = "en",
           country: str | None = None, code: str | None = None, site: str | None = None,
           role: str | None = None) -> QuerySpec:
        text = re.sub(r"\s+", " ", text).strip()
        return QuerySpec(text=text, category=cat, purpose=purpose, language=lang, country=country,
                         country_code=code if country else None, site=site, role=role,
                         freshness_days=config.publication_window_days)

    def _general(self, roles, targets, config, langs, skills, prefs) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        arr, emp = self._arrangement_word(config), self._employment_word(config)
        for role, (country, code) in itertools.product(roles, targets):
            where = country or "remote worldwide"
            out.append(self._q(f'"{role}" hiring {where} {arr} {emp}', SourceType.SEARCH_ENGINE, "general", config,
                               country=country, code=code, role=role))
            if "id" in langs and country == "Indonesia":
                out.append(self._q(f'lowongan kerja "{role}" {arr} {emp}', SourceType.SEARCH_ENGINE, "general",
                                   config, lang="id", country=country, code=code, role=role))
        if len(skills) >= 2:
            for (country, code) in targets[:3]:
                out.append(self._q(f'"{skills[0]}" "{skills[1]}" developer job {country or "remote"}',
                                   SourceType.SEARCH_ENGINE, "skills", config, country=country, code=code))
        for term in prefs.positive[:2]:
            out.append(self._q(f'"{roles[0]}" {term} hiring', SourceType.SEARCH_ENGINE, "preference", config,
                               role=roles[0]))
        # Rotate across geographies so a small budget still covers every country and remote.
        return _interleave_by_purpose(out, key=lambda q: f"{q.purpose}:{q.country}")

    def _portals(self, roles, targets, config) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        countries = {c for c, _ in targets if c}
        arr = self._arrangement_word(config)
        for portal in self.profiles.get("job_portals") or []:
            regions = set(portal.get("regions") or ["global"])
            if "global" in regions:
                applicable = [(c, code) for c, code in targets]
            else:
                applicable = [(c, code) for c, code in targets if c in regions]
                if not applicable and not (regions & countries):
                    continue
            for role in roles[:3]:
                for country, code in applicable[:2]:
                    where = country or "remote"
                    out.append(self._q(f'site:{portal["site"]} "{role}" {where} {arr}', SourceType.JOB_PORTAL,
                                       f"portal:{portal['name']}", config, country=country, code=code,
                                       site=portal["site"], role=role))
        return _interleave_by_purpose(out)

    def _social(self, roles, targets, config, langs) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        phrases_en = (self.profiles.get("hiring_phrases") or {}).get("en", ["we are hiring"])
        phrases_id = (self.profiles.get("hiring_phrases") or {}).get("id", ["lowongan"])
        for site in self.profiles.get("social_sites") or []:
            for i, role in enumerate(roles[:3]):
                phrase = phrases_en[i % len(phrases_en)]
                country, code = targets[i % len(targets)]
                out.append(self._q(f'site:{site["site"]} "{phrase}" "{role}" {country or "remote"}',
                                   SourceType.SOCIAL_MEDIA, f"social:{site['name']}", config, country=country,
                                   code=code, site=site["site"], role=role))
                if "id" in langs and config.wants_indonesia:
                    p_id = phrases_id[i % len(phrases_id)]
                    out.append(self._q(f'site:{site["site"]} "{p_id}" "{role}"', SourceType.SOCIAL_MEDIA,
                                       f"social:{site['name']}", config, lang="id", country="Indonesia",
                                       code="ID", site=site["site"], role=role))
        return _interleave_by_purpose(out)

    def _communities(self, roles, targets, config, langs, keywords) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        countries = {c for c, _ in targets if c}
        for site in self.profiles.get("community_sites") or []:
            regions = set(site.get("regions") or [])
            if regions and not regions & countries:
                continue
            for role in roles[:2]:
                if site["site"] == "reddit.com":
                    text = f'site:reddit.com "[hiring]" "{role}"'
                elif regions == {"Indonesia"}:
                    text = f'site:{site["site"]} lowongan "{role}"'
                else:
                    text = f'site:{site["site"]} hiring "{role}"'
                out.append(self._q(text, SourceType.COMMUNITY, f"community:{site['name']}", config,
                                   site=site["site"], role=role))
        if keywords:
            out.append(self._q(f'site:reddit.com/r/forhire "[Hiring]" {keywords[0]}', SourceType.COMMUNITY,
                               "community:Reddit", config, site="reddit.com"))
        return _interleave_by_purpose(out)

    def _ats(self, roles, targets, config) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        seen_sites: set[str] = set()
        for ats in self.profiles.get("ats_sites") or []:
            if ats["name"] in seen_sites and ats["name"] == "Greenhouse":
                continue
            seen_sites.add(ats["name"])
            for i, role in enumerate(roles[:3]):
                country, code = targets[i % len(targets)]
                out.append(self._q(f'site:{ats["site"]} "{role}" {country or "remote"}', SourceType.ATS,
                                   f"ats:{ats['name']}", config, site=ats["site"], role=role, country=country,
                                   code=code))
        return _interleave_by_purpose(out)

    def _careers(self, roles, targets, config) -> list[QuerySpec]:
        out: list[QuerySpec] = []
        exclude = "-site:linkedin.com -site:indeed.com -site:glassdoor.com -site:jobstreet.co.id"
        for role, (country, code) in itertools.product(roles[:3], targets[:3]):
            out.append(self._q(f'"{role}" careers "apply" {country or "remote"} {exclude}', SourceType.COMPANY_CAREERS,
                               "careers", config, country=country, code=code, role=role))
        return out

    # ----------------------------------------------------------------- main
    def generate(self, profile: CandidateProfile, config: SearchConfig, budget: int | None = None) -> QueryPlan:
        budget = int(budget if budget is not None else self.settings.get("budget.max_search_engine_queries", 40))
        roles = self.roles(profile, config)
        targets = self.country_targets(config)
        langs = config.languages or ["en"]
        keywords = self.keywords(profile, config)
        prefs = parse_preferences(config.additional_preferences)
        skills = [s for s in profile.frameworks + profile.programming_languages][:4]
        selected = set(config.source_categories)

        candidates: dict[SourceType, list[QuerySpec]] = {
            SourceType.SEARCH_ENGINE: self._general(roles, targets, config, langs, skills, prefs),
            SourceType.JOB_PORTAL: self._portals(roles, targets, config),
            SourceType.SOCIAL_MEDIA: self._social(roles, targets, config, langs),
            SourceType.COMMUNITY: self._communities(roles, targets, config, langs, keywords),
            SourceType.ATS: self._ats(roles, targets, config),
            SourceType.COMPANY_CAREERS: self._careers(roles, targets, config),
        }
        enabled = {k: v for k, v in candidates.items() if k.value in selected and v}
        plan = QueryPlan(keywords=keywords, role_terms=roles, preferences=prefs)
        if not enabled or budget <= 0:
            plan.queries = {k: [] for k in candidates}
            return plan
        total_share = sum(CATEGORY_SHARE[k] for k in enabled)
        alloc = {k: max(1, int(budget * CATEGORY_SHARE[k] / total_share)) for k in enabled}
        # Redistribute leftovers round-robin to categories that still have candidates.
        leftover = budget - sum(alloc.values())
        order = sorted(enabled, key=lambda k: -CATEGORY_SHARE[k])
        while leftover > 0 and any(alloc[k] < len(enabled[k]) for k in order):
            for k in order:
                if leftover > 0 and alloc[k] < len(enabled[k]):
                    alloc[k] += 1
                    leftover -= 1
        for k, qs in enabled.items():
            plan.queries[k] = _dedupe(qs)[: alloc[k]]
        return plan


def _dedupe(qs: list[QuerySpec]) -> list[QuerySpec]:
    seen: set[str] = set()
    out = []
    for q in qs:
        key = q.text.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out


def _interleave_by_purpose(qs: list[QuerySpec], key: Any = None) -> list[QuerySpec]:
    """Round-robin across purposes so a small budget still covers many platforms."""
    groups: dict[str, list[QuerySpec]] = {}
    for q in qs:
        groups.setdefault(key(q) if key else q.purpose, []).append(q)
    out: list[QuerySpec] = []
    for batch in itertools.zip_longest(*groups.values()):
        out.extend(q for q in batch if q is not None)
    return out


def describe_plan(plan: QueryPlan) -> dict[str, Any]:
    return {"keywords": plan.keywords, "roles": plan.role_terms, "per_category": plan.summary(),
            "preferences": {"positive": plan.preferences.positive, "negative": plan.preferences.negative}}
