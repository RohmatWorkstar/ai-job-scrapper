"""Duplicate detection across sources.

Certain duplicates (same external id, same canonical source/application URL, or
same company + near-identical title + compatible location + similar description)
are merged into one entry whose ``additional_sources`` keeps every source link.
Uncertain cases (same company + similar title but different known countries, or
dissimilar descriptions) are kept separate and cross-flagged as possible duplicates.
Different positions that merely share a title at different companies are never merged.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from src.core.models import JobOpportunity, SourceRef, SourceType
from src.extraction.job_normalizer import normalise_company
from src.utils.date_time import iso
from src.utils.validators import canonical_url

SOURCE_PRIORITY = {
    SourceType.ATS.value: 0,
    SourceType.COMPANY_CAREERS.value: 1,
    SourceType.JOB_PORTAL.value: 2,
    SourceType.SEARCH_ENGINE.value: 3,
    SourceType.COMMUNITY.value: 4,
    SourceType.SOCIAL_MEDIA.value: 5,
    SourceType.OTHER.value: 6,
}
_GENERIC_APPLY_HOSTS = ("linkedin.com", "indeed.com", "glints.com", "jobstreet")
_SENIORITY_WORDS = r"\b(senior|sr|junior|jr|lead|principal|staff|mid|intern|head|ii|iii|iv|i)\b"


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    t = re.sub(r"\b(remote|hybrid|on-?site|full[\s-]?time|contract|wfh)\b", " ", t)
    t = re.sub(r"[^a-z0-9+#.]+", " ", t)
    t = t.replace("full stack", "fullstack").replace("full-stack", "fullstack").replace("back end", "backend") \
        .replace("back-end", "backend").replace("front end", "frontend").replace("front-end", "frontend")
    return re.sub(r"\s+", " ", t).strip()


def seniority_key(title: str) -> str:
    return " ".join(sorted(set(re.findall(_SENIORITY_WORDS, title.lower()))))


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def text_similarity(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z]{4,}", a.lower()[:4000]))
    tb = set(re.findall(r"[a-z]{4,}", b.lower()[:4000]))
    if not ta or not tb:
        return -1.0  # unknown
    return len(ta & tb) / len(ta | tb)


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _completeness(j: JobOpportunity) -> int:
    fields = [j.company_name, j.location, j.country, j.published_at, j.application_url, j.salary_min,
              j.job_description[:50] if j.job_description else None]
    return sum(1 for f in fields if f) + (2 if j.work_arrangement != "unknown" else 0)


def _primary_order(j: JobOpportunity) -> tuple[int, int, int]:
    return (SOURCE_PRIORITY.get(j.source_type, 9), 0 if j.listed_in_live_feed else 1, -_completeness(j))


def _same_opportunity(a: JobOpportunity, b: JobOpportunity) -> str | None:
    """Return 'merge', 'possible' or None."""
    ca, cb = normalise_company(a.company_name), normalise_company(b.company_name)
    if not ca or not cb or ca != cb:
        return None
    if a.external_id and b.external_id:
        board_a, board_b = a.external_id.rsplit(":", 1)[0], b.external_id.rsplit(":", 1)[0]
        if board_a == board_b:
            return None  # different posting ids on the same board are different postings
    if a.source_name == b.source_name and a.listed_in_live_feed and b.listed_in_live_feed:
        return None
    if seniority_key(a.job_title) != seniority_key(b.job_title):
        return None
    ts = title_similarity(a.job_title, b.job_title)
    if ts < 0.85:
        return None
    if a.country and b.country and a.country != b.country:
        return "possible"
    la, lb = (a.city or "").lower(), (b.city or "").lower()
    if la and lb and la != lb:
        return "possible"
    ds = text_similarity(a.job_description, b.job_description)
    if ds == -1.0 or ds >= 0.45 or ts == 1.0 and ds >= 0.25:
        return "merge"
    return "possible"


def merge_group(jobs: list[JobOpportunity]) -> JobOpportunity:
    jobs = sorted(jobs, key=_primary_order)
    primary = jobs[0]
    known_urls = set(primary.all_source_urls)
    for other in jobs[1:]:
        for url, ref in [(other.source_url, SourceRef(other.source_type, other.source_name, other.source_url,
                                                     iso(other.discovered_at) or "", iso(other.published_at or other.post_created_at),
                                                     other.job_title, other.job_description[:200]))] + \
                        [(s.source_url, s) for s in other.additional_sources]:
            if url and url not in known_urls:
                primary.additional_sources.append(ref)
                known_urls.add(url)
        for attr in ("company_name", "company_website", "country", "city", "location", "application_url",
                     "salary_min", "salary_max", "salary_currency", "salary_period", "salary_text"):
            if not getattr(primary, attr) and getattr(other, attr):
                setattr(primary, attr, getattr(other, attr))
        for attr in ("work_arrangement", "employment_type", "seniority"):
            if getattr(primary, attr) == "unknown" and getattr(other, attr) != "unknown":
                setattr(primary, attr, getattr(other, attr))
        if not primary.published_at and other.published_at:
            primary.published_at = other.published_at
        if len(other.job_description) > len(primary.job_description) * 1.5:
            primary.job_description = other.job_description
        primary.application_emails = list(dict.fromkeys(primary.application_emails + other.application_emails))[:10]
        primary.required_skills = list(dict.fromkeys(primary.required_skills + other.required_skills))
        primary.preferred_skills = [s for s in dict.fromkeys(primary.preferred_skills + other.preferred_skills)
                                    if s not in primary.required_skills]
        primary.evidence.extend(e for e in other.evidence if e.field not in {"job_title"})
        primary.warnings = list(dict.fromkeys(primary.warnings + other.warnings))
        primary.listed_in_live_feed = primary.listed_in_live_feed or other.listed_in_live_feed
        primary.official_channel = primary.official_channel or other.official_channel
        primary.is_new = primary.is_new and other.is_new
    return primary


def deduplicate(jobs: list[JobOpportunity]) -> tuple[list[JobOpportunity], int]:
    """Return (unique opportunities, number of merged duplicate entries)."""
    n = len(jobs)
    uf = _UF(n)
    index: dict[str, int] = {}
    for i, j in enumerate(jobs):
        keys = []
        if j.external_id:
            keys.append("ext:" + j.external_id.lower())
        if j.source_url:
            keys.append("src:" + canonical_url(j.source_url))
        if j.application_url and not any(h in j.application_url for h in _GENERIC_APPLY_HOSTS):
            keys.append("app:" + canonical_url(j.application_url))
        for k in keys:
            if k in index:
                uf.union(index[k], i)
            else:
                index[k] = i
    # Fuzzy pass within the same normalised company.
    by_company: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        c = normalise_company(j.company_name)
        if c:
            by_company.setdefault(c, []).append(i)
    possible: list[tuple[int, int]] = []
    for idxs in by_company.values():
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                a, b = idxs[x], idxs[y]
                if uf.find(a) == uf.find(b):
                    continue
                verdict = _same_opportunity(jobs[a], jobs[b])
                if verdict == "merge":
                    uf.union(a, b)
                elif verdict == "possible":
                    possible.append((a, b))
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    merged: dict[int, JobOpportunity] = {}
    for root, members in groups.items():
        merged[root] = merge_group([jobs[i] for i in members]) if len(members) > 1 else jobs[members[0]]
    for a, b in possible:
        ja, jb = merged[uf.find(a)], merged[uf.find(b)]
        if ja is jb:
            continue
        if jb.job_id not in ja.possible_duplicates:
            ja.possible_duplicates.append(jb.job_id)
        if ja.job_id not in jb.possible_duplicates:
            jb.possible_duplicates.append(ja.job_id)
    unique = list(merged.values())
    for j in unique:
        if j.possible_duplicates:
            msg = "Possibly the same opening as another entry (same company, similar title) - check before applying twice."
            if msg not in j.warnings:
                j.warnings.append(msg)
    return unique, n - len(unique)
