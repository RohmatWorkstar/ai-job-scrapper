"""Normalisation of raw discoveries into :class:`JobOpportunity` records with evidence.

Rules: unknown values stay ``None``/``"unknown"``; the original source URL, title,
snippet and provider fields are preserved (as evidence and in raw_discovery.json).
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

from src.ai.prompts import job_extraction_prompt
from src.ai.provider_manager import AIManager
from src.core.configuration import Settings
from src.core.models import ContentKind, Evidence, JobOpportunity, RawDiscovery
from src.extraction.content_parser import (
    CountryDetector,
    SkillDetector,
    clean_title,
    detect_employment_type,
    detect_seniority,
    detect_work_arrangement,
    guess_city,
    normalise_arrangement,
    normalise_currency,
    normalise_employment_type,
    normalise_seniority,
    parse_salary,
)
from src.extraction.html_extractor import PageExtraction
from src.utils.date_time import iso, parse_datetime, utc_now
from src.utils.validators import canonical_url, clean_text

_PREFERRED_SPLIT = re.compile(r"(?i)\b(nice[\s-]to[\s-]have|preferred(?:\s+qualifications)?|bonus\s+points?|"
                              r"good\s+to\s+have|plus\s+points?|nilai\s+tambah|diutamakan)\b")
_COMPANY_SUFFIX = re.compile(r"(?i)[,\s]+(pt\.?|tbk\.?|inc\.?|llc|ltd\.?|limited|gmbh|b\.v\.|bv|s\.a\.|pte\.?\s*ltd\.?|"
                             r"sdn\.?\s*bhd\.?|corp\.?|corporation|co\.?)$")


def normalise_company(name: str | None) -> str:
    if not name:
        return ""
    n = re.sub(r"(?i)^pt\.?\s+", "", name.strip())
    for _ in range(3):
        n = _COMPANY_SUFFIX.sub("", n).strip()
    return re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()


def make_job_id(d: RawDiscovery) -> str:
    key = d.external_id or canonical_url(d.source_url) or f"{d.source_name}|{d.title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def enrich_with_page(d: RawDiscovery, page: PageExtraction) -> None:
    """Merge a fetched page's extraction into a discovery (keeps original search data)."""
    d.raw["fetch"] = {
        "status": page.status, "ok": page.ok, "error": page.error, "error_kind": page.error_kind,
        "final_url": page.final_url, "closed_markers": page.closed_markers, "fetched_at": iso(page.retrieved_at),
        "has_job_posting_ld": bool(page.job_postings), "apply_links": page.apply_links[:3],
        "browser": page.rendered_with_browser,
    }
    if not page.ok:
        return
    jp = page.primary_posting
    if jp:
        d.raw["fetch"]["valid_through"] = iso(jp.valid_through) if jp.valid_through else None
        d.title = jp.title or d.title
        d.text = jp.description or d.text
        d.company = jp.company or d.company
        d.company_website = jp.company_url or d.company_website
        d.location = "; ".join(jp.locations) or d.location
        d.country = jp.country or d.country
        if jp.date_posted:
            d.published_at = jp.date_posted
            d.published_at_evidence = "schema.org JobPosting.datePosted on the fetched page"
        if jp.employment_type:
            d.employment_type = jp.employment_type[0]
        if jp.remote:
            d.work_arrangement = "remote"
        if jp.salary_min:
            d.salary_min, d.salary_max, d.salary_currency, d.salary_period = (
                jp.salary_min, jp.salary_max, jp.salary_currency, jp.salary_period)
        if jp.applicant_locations:
            d.location_restrictions = jp.applicant_locations
        d.structured = True
        if d.content_kind == ContentKind.SEARCH_RESULT:
            d.content_kind = ContentKind.JOB_POSTING
    else:
        if page.text and len(page.text) > len(d.text):
            d.text = page.text[:8000]
        if page.published_hint and not d.published_at_evidence.startswith("schema"):
            d.published_at = page.published_hint
            d.published_at_evidence = f"page metadata ({page.published_evidence})"
    if page.apply_links and not d.application_url:
        d.application_url = page.apply_links[0]


class JobNormalizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skills = SkillDetector(settings.taxonomy)
        self.countries = CountryDetector(settings.country_table())
        self.max_desc = int(settings.get("extraction.max_description_chars", 6000))

    def normalise(self, d: RawDiscovery) -> JobOpportunity:
        retrieved = iso(d.retrieved_at) or iso(utc_now()) or ""
        ev: list[Evidence] = []
        method = "api_field" if d.structured and d.listed_in_live_feed else (
            "json_ld" if d.structured else "search_snippet")

        def add(field: str, value: Any, excerpt: str, m: str = method) -> None:
            if value not in (None, "", []):
                ev.append(Evidence(field, str(value)[:200], clean_text(excerpt, 300), d.source_url, retrieved, m))

        title = clean_title(html.unescape(d.title or "")) or "(untitled)"
        add("job_title", title, d.title)
        text = d.text or d.snippet or ""
        company = html.unescape(d.company or "").strip() or None
        if company:
            add("company_name", company, company)
        location = clean_text(html.unescape(d.location or ""), 200) or None
        country = self.countries.normalise(d.country) if d.country else None
        if not country:
            country = self.countries.detect(location, title)
        if location:
            add("location", location, location)
        arrangement = normalise_arrangement(d.work_arrangement) if d.work_arrangement else "unknown"
        if arrangement == "unknown":
            arrangement = detect_work_arrangement(title, location, d.snippet, text[:3000])
            if arrangement != "unknown":
                add("work_arrangement", arrangement, _excerpt_for(arrangement, " ".join([title, location or "", text[:3000]])),
                    "html_text")
        employment = normalise_employment_type(d.employment_type) if d.employment_type else "unknown"
        if employment == "unknown":
            employment = detect_employment_type(title, text[:3000])
        seniority = normalise_seniority(d.seniority) if d.seniority else "unknown"
        if seniority == "unknown":
            seniority = detect_seniority(title, text)

        sal_min, sal_max, cur, period, sal_text = d.salary_min, d.salary_max, normalise_currency(d.salary_currency), \
            d.salary_period, d.salary_text
        if sal_min is None:
            parsed = parse_salary(d.salary_text or "") or parse_salary(text[:6000]) or parse_salary(d.snippet)
            if parsed:
                sal_min, sal_max, cur, period, sal_text = parsed.minimum, parsed.maximum, parsed.currency, parsed.period, parsed.text
        if sal_min is not None or sal_text:
            add("salary", sal_text or f"{cur} {sal_min}-{sal_max}", sal_text or f"{sal_min}-{sal_max} {cur}")

        req, pref = self._skills(text)
        published = d.published_at
        post_created = d.post_created_at
        warnings: list[str] = []
        if d.content_kind in (ContentKind.SOCIAL_POST, ContentKind.COMMUNITY_POST):
            # A post date is not a job publication date: keep them separate.
            post_created = post_created or published
            published = None if "post date" in d.published_at_evidence or "search index" in d.published_at_evidence else published
        if published or post_created:
            add("published_at" if published else "post_created_at", iso(published or post_created),
                d.published_at_evidence or "source date field",
                "search_snippet" if "search index" in d.published_at_evidence else method)
        if (published or post_created) and "search index" in d.published_at_evidence:
            warnings.append("Date shown is the search engine's page date, not a confirmed posting date.")
        if d.application_url:
            add("application_url", d.application_url, d.application_url)

        job = JobOpportunity(
            job_id=make_job_id(d),
            job_title=title,
            company_name=company,
            company_website=d.company_website,
            country=country,
            city=guess_city(location, country),
            location=location,
            work_arrangement=arrangement,
            employment_type=employment,
            seniority=seniority,
            salary_min=sal_min,
            salary_max=sal_max,
            salary_currency=cur,
            salary_period=period,
            salary_text=sal_text,
            job_description=clean_text(text, self.max_desc),
            required_skills=req,
            preferred_skills=pref,
            published_at=published,
            post_created_at=post_created,
            discovered_at=d.retrieved_at,
            source_type=d.source_type.value,
            source_name=d.source_name,
            source_url=d.source_url,
            application_url=d.application_url,
            evidence=ev,
            warnings=warnings,
            content_kind=d.content_kind.value,
            official_channel=d.official_channel,
            listed_in_live_feed=d.listed_in_live_feed,
            external_id=d.external_id,
            location_restrictions=list(d.location_restrictions),
            query=d.query,
            page_fetched=bool((d.raw.get("fetch") or {}).get("ok")),
        )
        if not company:
            job.warnings.append("Company could not be identified from the source.")
        return job

    def _skills(self, text: str) -> tuple[list[str], list[str]]:
        if not text:
            return [], []
        m = _PREFERRED_SPLIT.search(text)
        if not m:
            return self.skills.detect_names(text), []
        before, after = text[: m.start()], text[m.start():]
        req = self.skills.detect_names(before)
        pref = [s for s in self.skills.detect_names(after) if s not in req]
        return req, pref


def _excerpt_for(word: str, text: str) -> str:
    m = re.search(rf"(?i).{{0,60}}\b{re.escape(word[:5])}.{{0,60}}", text)
    return m.group(0) if m else word


# ----------------------------------------------------------- AI extraction assist

def _supported(value: str | None, source_text: str) -> bool:
    if not value:
        return False
    v = re.sub(r"\s+", " ", value.lower()).strip()
    return len(v) >= 2 and v in re.sub(r"\s+", " ", source_text.lower())


async def ai_extract(discoveries: list[RawDiscovery], ai: AIManager, batch_size: int = 8, max_items: int = 80) -> int:
    """Use the AI provider to interpret unstructured discoveries. Every returned value must
    literally appear in the source text; unsupported values are discarded. Returns #items updated."""
    if not ai.available:
        return 0
    targets = [d for d in discoveries if not d.structured][:max_items]
    updated = 0
    for start in range(0, len(targets), batch_size):
        batch = targets[start:start + batch_size]
        items = [{"id": str(i), "url": d.source_url, "title": d.title,
                  "text": (d.text or d.snippet)[:1800]} for i, d in enumerate(batch)]
        result = await ai.ask_json("job_extraction", job_extraction_prompt(items), cache_days=3)
        if not isinstance(result, dict):
            if ai.remaining <= 0 or not ai.available:
                break
            continue
        for item in result.get("items", []) or []:
            try:
                d = batch[int(item.get("id"))]
            except (TypeError, ValueError, IndexError):
                continue
            source_text = f"{d.title}\n{d.snippet}\n{d.text}"
            kind = str(item.get("kind") or "")
            d.raw["ai"] = {"kind": kind, "is_job_opportunity": item.get("is_job_opportunity")}
            if kind == "not_a_job" or item.get("is_job_opportunity") is False:
                d.raw["ai_not_a_job"] = True
            for field_name, attr in (("job_title", "title"), ("company_name", "company"), ("location", "location")):
                val = item.get(field_name)
                if isinstance(val, str) and _supported(val, source_text):
                    if attr == "title" and d.title and len(val) >= len(clean_title(d.title)):
                        continue
                    if attr != "title" and getattr(d, attr):
                        continue
                    setattr(d, attr, val.strip())
                    d.raw.setdefault("ai_fields", []).append(field_name)
            for field_name, attr in (("work_arrangement", "work_arrangement"), ("employment_type", "employment_type")):
                val = item.get(field_name)
                if isinstance(val, str) and val and not getattr(d, attr):
                    # Enumerations must also be explicitly supported by the text.
                    probe = {"onsite": "site", "full_time": "full", "part_time": "part"}.get(val, val.split("_")[0])
                    if _supported(probe, source_text):
                        setattr(d, attr, val)
            date_txt = item.get("published_date_text")
            if isinstance(date_txt, str) and _supported(date_txt, source_text):
                dt = parse_datetime(date_txt)
                if dt:
                    if d.content_kind in (ContentKind.SOCIAL_POST, ContentKind.COMMUNITY_POST):
                        d.post_created_at = dt
                    d.published_at = dt
                    d.published_at_evidence = f"date text in source: '{date_txt[:60]}'"
            updated += 1
    return updated
