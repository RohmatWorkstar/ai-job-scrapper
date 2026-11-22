"""HTML extraction: schema.org JobPosting JSON-LD, metadata, visible text, dates, apply links.

Fetched content is untrusted. Nothing in a page can alter application behaviour:
we only read data fields and text; scripts are never executed (except in the
optional, sandboxed browser renderer) and links are never followed automatically.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.core.models import ContentKind, RawDiscovery, SourceType
from src.extraction.content_parser import normalise_currency
from src.utils.date_time import parse_datetime, utc_now
from src.utils.http_client import HttpResult, SafeHttpClient
from src.utils.validators import clean_text, domain_matches

_APPLY_RE = re.compile(r"(?i)\b(apply|application|lamar|kirim\s+lamaran|submit\s+(?:your\s+)?(?:cv|resume))\b")
_POSTED_RE = re.compile(
    r"(?i)(?:posted|published|diposting|dipublikasikan|date\s+posted|tanggal\s+posting)\s*(?:on|:|pada)?\s*"
    r"([A-Za-z0-9 ,./-]{3,40}?(?:ago|lalu|\d{4}))"
)


def html_to_text(fragment: str | None, limit: int | None = None) -> str:
    if not fragment:
        return ""
    unescaped = html_lib.unescape(fragment) if "&lt;" in fragment else fragment
    soup = BeautifulSoup(unescaped, "html.parser")
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        br.insert_after("\n")
    return clean_text(soup.get_text(" "), limit)


@dataclass
class JobPostingLD:
    title: str = ""
    description: str = ""
    date_posted: datetime | None = None
    valid_through: datetime | None = None
    company: str | None = None
    company_url: str | None = None
    locations: list[str] = field(default_factory=list)
    country: str | None = None
    remote: bool = False
    applicant_locations: list[str] = field(default_factory=list)
    employment_type: list[str] = field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    url: str | None = None
    identifier: str | None = None
    direct_apply: bool | None = None


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _iter_ld_objects(data: Any):
    for item in _as_list(data):
        if isinstance(item, dict):
            if "@graph" in item:
                yield from _iter_ld_objects(item["@graph"])
            yield item


def parse_job_posting_ld(obj: dict[str, Any]) -> JobPostingLD:
    jp = JobPostingLD()
    jp.title = clean_text(html_lib.unescape(str(obj.get("title") or obj.get("name") or "")), 200)
    jp.description = html_to_text(str(obj.get("description") or ""), 8000)
    jp.date_posted = parse_datetime(obj.get("datePosted"))
    vt = obj.get("validThrough")
    if vt:
        try:
            jp.valid_through = datetime.fromisoformat(str(vt).replace("Z", "+00:00"))
            if jp.valid_through.tzinfo is None:
                from datetime import timezone
                jp.valid_through = jp.valid_through.replace(tzinfo=timezone.utc)
        except ValueError:
            jp.valid_through = None
    org = obj.get("hiringOrganization")
    if isinstance(org, dict):
        jp.company = clean_text(str(org.get("name") or ""), 120) or None
        jp.company_url = org.get("sameAs") or org.get("url")
        if isinstance(jp.company_url, list):
            jp.company_url = jp.company_url[0] if jp.company_url else None
    elif isinstance(org, str):
        jp.company = org
    for loc in _as_list(obj.get("jobLocation")):
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
            text = ", ".join(str(p) for p in parts if p)
            if text:
                jp.locations.append(text)
            if country and not jp.country:
                jp.country = str(country)
        elif isinstance(addr, str):
            jp.locations.append(addr)
    if str(obj.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        jp.remote = True
    for req in _as_list(obj.get("applicantLocationRequirements")):
        if isinstance(req, dict) and req.get("name"):
            jp.applicant_locations.append(str(req["name"]))
    jp.employment_type = [str(e) for e in _as_list(obj.get("employmentType"))]
    sal = obj.get("baseSalary")
    if isinstance(sal, dict):
        jp.salary_currency = normalise_currency(sal.get("currency"))
        val = sal.get("value")
        if isinstance(val, dict):
            jp.salary_min = _num(val.get("minValue") or val.get("value"))
            jp.salary_max = _num(val.get("maxValue"))
            jp.salary_period = (str(val.get("unitText") or "").lower() or None)
        else:
            jp.salary_min = _num(val)
    jp.url = obj.get("url")
    ident = obj.get("identifier")
    if isinstance(ident, dict):
        jp.identifier = str(ident.get("value") or "") or None
    elif ident:
        jp.identifier = str(ident)
    if "directApply" in obj:
        jp.direct_apply = bool(obj.get("directApply"))
    return jp


@dataclass
class PageExtraction:
    url: str
    final_url: str = ""
    status: int | None = None
    ok: bool = False
    error: str | None = None
    error_kind: str | None = None
    title: str = ""
    meta_description: str = ""
    text: str = ""
    lang: str | None = None
    job_postings: list[JobPostingLD] = field(default_factory=list)
    apply_links: list[str] = field(default_factory=list)
    published_hint: datetime | None = None
    published_evidence: str = ""
    closed_markers: list[str] = field(default_factory=list)
    retrieved_at: datetime = field(default_factory=utc_now)
    rendered_with_browser: bool = False

    @property
    def primary_posting(self) -> JobPostingLD | None:
        return self.job_postings[0] if self.job_postings else None

    def to_discovery(self, category: SourceType, source_name: str, source_url: str) -> RawDiscovery:
        jp = self.primary_posting
        d = RawDiscovery(
            source_type=category, source_name=source_name, source_url=source_url,
            title=(jp.title if jp and jp.title else self.title), retrieved_at=self.retrieved_at,
            snippet=self.meta_description, text=(jp.description if jp and jp.description else self.text),
            structured=bool(jp),
        )
        if jp:
            d.company = jp.company
            d.company_website = jp.company_url
            d.location = "; ".join(jp.locations) or None
            d.country = jp.country
            d.published_at = jp.date_posted
            d.published_at_evidence = "schema.org JobPosting.datePosted" if jp.date_posted else ""
            d.employment_type = jp.employment_type[0] if jp.employment_type else None
            d.work_arrangement = "remote" if jp.remote else None
            d.salary_min, d.salary_max, d.salary_currency, d.salary_period = (
                jp.salary_min, jp.salary_max, jp.salary_currency, jp.salary_period)
            d.location_restrictions = jp.applicant_locations
            d.external_id = jp.identifier
            d.raw["valid_through"] = jp.valid_through.isoformat() if jp.valid_through else None
        elif self.published_hint:
            d.published_at = self.published_hint
            d.published_at_evidence = self.published_evidence
        if self.apply_links:
            d.application_url = self.apply_links[0]
        d.content_kind = ContentKind.JOB_POSTING if jp else ContentKind.SEARCH_RESULT
        d.language = self.lang
        return d


def extract_from_html(html: str, url: str, closed_indicators: list[str] | None = None) -> PageExtraction:
    page = PageExtraction(url=url, final_url=url, ok=True)
    soup = BeautifulSoup(html, "html.parser")
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        page.lang = str(html_tag.get("lang"))[:10]
    # JSON-LD (parsed as data only, never executed)
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        for obj in _iter_ld_objects(data):
            types = [str(t) for t in _as_list(obj.get("@type"))]
            if "JobPosting" in types:
                page.job_postings.append(parse_job_posting_ld(obj))
    og = soup.find("meta", attrs={"property": "og:title"})
    page.title = clean_text((og.get("content") if og else None) or (soup.title.string if soup.title else "") or "", 200)
    if not page.title:
        h1 = soup.find("h1")
        page.title = clean_text(h1.get_text(" ") if h1 else "", 200)
    desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    page.meta_description = clean_text(desc.get("content") if desc else "", 500)
    for meta_name in ("article:published_time", "og:published_time", "datePublished", "date"):
        m = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
        if m and m.get("content"):
            dt = parse_datetime(m.get("content"))
            if dt:
                page.published_hint, page.published_evidence = dt, f"meta {meta_name}={m.get('content')}"
                break
    # Apply links
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        label = a.get_text(" ", strip=True)
        if href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        if _APPLY_RE.search(label) or re.search(r"(?i)/apply|/application|applyurl", href):
            absolute = urljoin(url, href)
            if absolute.startswith("http") and absolute not in page.apply_links:
                page.apply_links.append(absolute)
        if len(page.apply_links) >= 5:
            break
    # Visible text
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    page.text = clean_text(soup.get_text("\n"), 12000)
    if not page.published_hint:
        t = soup.find("time", attrs={"datetime": True})
        dt = parse_datetime(t.get("datetime")) if t else None
        if dt:
            page.published_hint, page.published_evidence = dt, f"<time datetime={t.get('datetime')}>"
        else:
            m = _POSTED_RE.search(page.text[:5000])
            if m:
                dt = parse_datetime(m.group(1).strip())
                if dt:
                    page.published_hint, page.published_evidence = dt, m.group(0).strip()[:120]
    lower = page.text.lower()
    for marker in closed_indicators or []:
        if marker.lower() in lower:
            page.closed_markers.append(marker)
    return page


async def fetch_and_extract(http: SafeHttpClient, url: str, settings: Any, *, allow_browser: bool = True) -> PageExtraction:
    """Fetch a page under policy (no-fetch domains, robots.txt, SSRF) and extract it."""
    no_fetch = settings.get("extraction.no_fetch_domains", []) or []
    if domain_matches(url, no_fetch):
        return PageExtraction(url=url, ok=False, error="Not fetched: platform requires login / restricts automated access",
                              error_kind="policy")
    res: HttpResult = await http.get(url, respect_robots=True, headers={"Accept": "text/html,application/xhtml+xml"})
    if not res.ok:
        return PageExtraction(url=url, final_url=res.final_url, status=res.status, ok=False, error=res.error,
                              error_kind=res.error_kind)
    closed = settings.get("verification.closed_indicators", []) or []
    page = extract_from_html(res.text, res.final_url or url, closed)
    page.status = res.status
    page.final_url = res.final_url or url
    if allow_browser and settings.get("extraction.use_browser_rendering", False) and not page.job_postings and len(page.text) < 300:
        from src.extraction.browser_extractor import BrowserRenderer

        rendered = await BrowserRenderer.shared().render(url, http)
        if rendered:
            page2 = extract_from_html(rendered, url, closed)
            page2.status, page2.final_url, page2.rendered_with_browser = res.status, page.final_url, True
            return page2
    return page
