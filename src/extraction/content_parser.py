"""Deterministic text interpretation shared by CV analysis and job extraction.

Every function only reports what is literally present in the text and returns
``None``/``"unknown"``/empty lists otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

# --------------------------------------------------------------------- skills


@dataclass(frozen=True)
class SkillHit:
    name: str
    category: str
    excerpt: str


class SkillDetector:
    """Taxonomy-driven skill detector (aliases are case-insensitive, patterns case-sensitive)."""

    def __init__(self, taxonomy: dict[str, Any]) -> None:
        self._entries: list[tuple[str, str, list[re.Pattern[str]]]] = []
        for category, items in (taxonomy or {}).items():
            for name, spec in (items or {}).items():
                spec = spec or {}
                pats: list[re.Pattern[str]] = []
                exclude = spec.get("exclude_following") or []
                neg = "".join(f"(?!{re.escape(x)})" for x in exclude)
                for alias in spec.get("aliases") or []:
                    pats.append(re.compile(rf"(?<![\w+#.]){re.escape(str(alias))}(?![\w+#]){neg}", re.IGNORECASE))
                for p in spec.get("patterns") or []:
                    pats.append(re.compile(p))
                self._entries.append((str(name), str(category), pats))

    @property
    def names(self) -> list[str]:
        return [n for n, _, _ in self._entries]

    def category_of(self, name: str) -> str | None:
        for n, c, _ in self._entries:
            if n.lower() == name.lower():
                return c
        return None

    def detect(self, text: str) -> list[SkillHit]:
        hits: list[SkillHit] = []
        if not text:
            return hits
        for name, category, pats in self._entries:
            for pat in pats:
                m = pat.search(text)
                if m:
                    s, e = max(0, m.start() - 60), min(len(text), m.end() + 60)
                    excerpt = re.sub(r"\s+", " ", text[s:e]).strip()
                    hits.append(SkillHit(name, category, excerpt))
                    break
        return hits

    def detect_names(self, text: str) -> list[str]:
        return [h.name for h in self.detect(text)]

    def canonical(self, raw: str) -> str | None:
        """Map a free-form skill string (e.g. from AI output) to a taxonomy name."""
        raw = raw.strip()
        if not raw:
            return None
        for name, _, pats in self._entries:
            if name.lower() == raw.lower():
                return name
            for pat in pats:
                m = pat.fullmatch(raw) or (pat.flags & re.IGNORECASE and pat.search(raw) and len(raw) <= len(name) + 12)
                if m:
                    return name
        return None


# --------------------------------------------------------------- arrangement

_REMOTE_RE = re.compile(
    r"(?i)\b(fully[\s-]remote|100%\s*remote|remote[\s-](?:first|friendly|only|ok|role|position|job|work)|work\s+from\s+home|wfh|"
    r"work\s+from\s+anywhere|wfa|remote)\b"
)
_HYBRID_RE = re.compile(r"(?i)\bhybrid\b|\bflexible\s+(?:working|work)\s+arrangement")
_ONSITE_RE = re.compile(r"(?i)\b(on[\s-]?site|in[\s-]office|work\s+from\s+office|wfo|office[\s-]based)\b")
_NOT_REMOTE_RE = re.compile(r"(?i)\b(not\s+(?:a\s+)?remote|no\s+remote|non[\s-]remote)\b")


def detect_work_arrangement(*texts: str | None) -> str:
    """Return remote | hybrid | onsite | unknown based on explicit wording only."""
    joined = " \n ".join(t for t in texts if t)
    if not joined:
        return "unknown"
    if _HYBRID_RE.search(joined):
        return "hybrid"
    if _NOT_REMOTE_RE.search(joined):
        return "onsite"
    if _REMOTE_RE.search(joined):
        return "remote"
    if _ONSITE_RE.search(joined):
        return "onsite"
    return "unknown"


def normalise_arrangement(value: str | None) -> str:
    if not value:
        return "unknown"
    v = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if v in {"remote", "fullyremote", "telecommute", "wfh", "remoteok", "anywhere"}:
        return "remote"
    if v in {"hybrid", "flexible"}:
        return "hybrid"
    if v in {"onsite", "inoffice", "office", "inperson", "wfo"}:
        return "onsite"
    return detect_work_arrangement(value)


# ----------------------------------------------------------- employment type

_EMPLOYMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("internship", re.compile(r"(?i)\b(intern(ship)?|magang|trainee|graduate\s+program|working\s+student|werkstudent)\b")),
    ("freelance", re.compile(r"(?i)\b(freelance|freelancer|gig|project[\s-]based|self[\s-]employed)\b")),
    ("contract", re.compile(r"(?i)\b(contract(or)?|kontrak|pkwt|fixed[\s-]term|temporary|temp)\b")),
    ("part_time", re.compile(r"(?i)\b(part[\s-]?time|paruh\s+waktu|teilzeit)\b")),
    ("full_time", re.compile(r"(?i)\b(full[\s-]?time|permanent|purna\s+waktu|pkwtt|vollzeit|regular\s+employee)\b")),
]


def detect_employment_type(*texts: str | None) -> str:
    joined = " \n ".join(t for t in texts if t)
    for name, pat in _EMPLOYMENT_PATTERNS:
        if pat.search(joined):
            return name
    return "unknown"


def normalise_employment_type(value: str | Iterable[str] | None) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        vals = [normalise_employment_type(v) for v in value]
        vals = [v for v in vals if v != "unknown"]
        return vals[0] if vals else "unknown"
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "full_time": "full_time", "fulltime": "full_time", "permanent": "full_time", "regular": "full_time",
        "contract": "contract", "contractor": "contract", "temporary": "contract", "temp": "contract",
        "part_time": "part_time", "parttime": "part_time",
        "intern": "internship", "internship": "internship",
        "freelance": "freelance", "freelancer": "freelance", "per_diem": "contract", "volunteer": "unknown",
    }
    return mapping.get(v, detect_employment_type(value))


# ------------------------------------------------------------------ seniority

SENIORITY_ORDER = ["entry", "junior", "mid", "senior", "lead"]
_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("lead", re.compile(r"(?i)\b(lead|principal|staff|head\s+of|engineering\s+manager|architect|director|vp|chief|supervisor|team\s+lead|tech\s+lead)\b")),
    ("senior", re.compile(r"(?i)\b(senior|sr\.?|snr)\b|\b(?:engineer|developer)\s+(?:iii|3)\b")),
    ("entry", re.compile(r"(?i)\b(intern(ship)?|graduate|entry[\s-]level|fresh\s*graduate|fresher|trainee|magang|new\s+grad)\b")),
    ("junior", re.compile(r"(?i)\b(junior|jr\.?|associate)\b|\b(?:engineer|developer)\s+(?:i|1)\b")),
    ("mid", re.compile(r"(?i)\b(mid[\s-]?level|mid[\s-]?senior|intermediate|medior)\b|\b(?:engineer|developer)\s+(?:ii|2)\b")),
]


_DESC_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("lead", re.compile(r"(?i)(?:seeking|looking\s+for|hiring|role\s*(?:is|level)|level|seniority)\s*[:\-]?\s*(?:a\s+|an\s+)?(?:tech\s+lead|team\s+lead|principal|staff\s+(?:software\s+)?engineer|lead\s+(?:software\s+|backend\s+|frontend\s+|fullstack\s+)?(?:engineer|developer))\b")),
    ("senior", re.compile(r"(?i)(?:seeking|looking\s+for|hiring|role\s*(?:is|level)|level|seniority)\s*[:\-]?\s*(?:a\s+|an\s+)?(?:senior|sr\.?)\b")),
    ("entry", re.compile(r"(?i)\b(?:entry[\s-]level|fresh\s*graduate|new\s+grad|magang)\b|(?:seeking|looking\s+for|hiring|level|seniority)\s*[:\-]?\s*(?:a\s+|an\s+)?(?:intern(?:ship)?|graduate|trainee)\b")),
    ("junior", re.compile(r"(?i)(?:seeking|looking\s+for|hiring|role\s*(?:is|level)|level|seniority)\s*[:\-]?\s*(?:a\s+|an\s+)?(?:junior|jr\.?|associate)\b")),
    ("mid", re.compile(r"(?i)\bmid[\s-]?level\b|(?:seeking|looking\s+for|hiring|role\s*(?:is|level)|level|seniority)\s*[:\-]?\s*(?:a\s+|an\s+)?(?:mid[\s-]?senior|intermediate|medior)\b")),
]


def detect_seniority(title: str | None, description: str | None = None) -> str:
    if title:
        for name, pat in _SENIORITY_PATTERNS:
            if pat.search(title):
                return name
    desc = (description or "")[:1500]
    if desc:
        for name, pat in _DESC_SENIORITY_PATTERNS:
            if pat.search(desc):
                return name
    return "unknown"


def normalise_seniority(value: str | None) -> str:
    if not value:
        return "unknown"
    v = value.strip().lower()
    mapping = {
        "internship": "entry", "intern": "entry", "entry level": "entry", "entry_level": "entry", "entry": "entry",
        "graduate": "entry", "junior": "junior", "associate": "junior", "mid": "mid", "mid level": "mid",
        "mid-level": "mid", "mid_level": "mid", "intermediate": "mid", "mid_senior_level": "senior",
        "mid-senior level": "senior", "senior": "senior", "lead": "lead", "manager": "lead", "director": "lead",
        "executive": "lead", "principal": "lead", "staff": "lead",
    }
    return mapping.get(v, detect_seniority(value))


_YEARS_REQ_RE = re.compile(
    r"(?i)(?:minimum|min\.?|at\s+least|minimal)?\s*(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:\+\s*)?(?:years?|yrs?|tahun)\b"
    r"(?:\s+(?:of\s+)?(?:professional\s+|relevant\s+|hands[\s-]on\s+|working\s+)?(?:experience|exp|pengalaman))"
)


def detect_years_required(text: str | None) -> int | None:
    if not text:
        return None
    values = [int(m.group(1)) for m in _YEARS_REQ_RE.finditer(text) if 0 < int(m.group(1)) <= 25]
    return min(values) if values else None


# --------------------------------------------------------------------- salary

_CURRENCY_SYMBOLS = {
    "rp": "IDR", "idr": "IDR", "$": "USD", "usd": "USD", "us$": "USD", "sgd": "SGD", "s$": "SGD", "€": "EUR",
    "eur": "EUR", "£": "GBP", "gbp": "GBP", "myr": "MYR", "rm": "MYR", "aud": "AUD", "a$": "AUD", "cad": "CAD",
    "c$": "CAD", "jpy": "JPY", "¥": "JPY", "inr": "INR", "₹": "INR", "aed": "AED", "php": "PHP", "₱": "PHP",
    "chf": "CHF", "hkd": "HKD", "nzd": "NZD", "thb": "THB", "vnd": "VND", "krw": "KRW",
}
_CUR_ALT = "|".join(sorted((re.escape(k) for k in _CURRENCY_SYMBOLS), key=len, reverse=True))
_NUM = r"\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_MULT = r"(?:\s?(?:k|K|rb|ribu|jt|juta|m|M|mn|mio|million|billion|miliar)\b)?"
_SALARY_RE = re.compile(
    rf"(?i)(?<![\w])({_CUR_ALT})\s?({_NUM}){_MULT}(?:\s*(?:-|–|—|to|sampai|s/d|s\.d\.)\s*(?:{_CUR_ALT})?\s?({_NUM}))?({_MULT})"
    rf"(?:\s*(?:/|per|a)\s*(hour|hr|month|mo|bulan|bln|year|yr|annum|annual|tahun|day|week)\b)?"
)
_JUTA_RE = re.compile(rf"(?i)\b({_NUM})\s*(?:-|–|to|sampai)?\s*({_NUM})?\s*(jt|juta)\b")
_NON_SALARY_CTX = re.compile(
    r"(?i)\b(series\s+[a-f]|funding|funded|raised|raise|valuation|revenue|arr|mrr|gm[vw]|market\s+cap|"
    r"users?|pengguna|customers?|transaksi|transactions?|downloads?|invested|investment|aum|assets?|"
    r"capital|backed|trillion|triliun|employees?|clients?|merchants?|visits?|views?|orders?)\b"
)
_SALARY_CUE_RE = re.compile(
    r"(?i)\b(salary|compensation|pay(?:\s+range)?|base|ote|remuneration|remunerasi|gaji|upah|kompensasi|"
    r"penghasilan|take\s+home|thp|stipend|rate|kisaran)\b|/(?:yr|year|month|mo|hour|hr|bln|bulan|tahun)\b"
)
_SALARY_CTX_ID = re.compile(
    r"(?i)\b(gaji|salary|upah|kompensasi|penghasilan|take\s+home|thp|remunerasi|kisaran|range|rate|"
    r"rp|idr|bulan|bln|per\s+bulan|gross|nett?)\b|/bln|/bulan"
)
_MAX_SALARY_BY_CURRENCY = {
    "IDR": 2_000_000_000.0,
    "VND": 5_000_000_000.0,
    "KRW": 200_000_000.0,
    "JPY": 200_000_000.0,
    "INR": 100_000_000.0,
}
_DEFAULT_MAX_SALARY = 2_000_000.0


def _to_number(token: str, mult: str | None) -> float | None:
    t = token.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        t = re.sub(r"[.,]", "", t)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+[.,]\d{1,2}", t):
        t = re.sub(r"[.,](?=\d{3})", "", t).replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    m = (mult or "").strip().lower()
    if m in {"k", "rb", "ribu"}:
        value *= 1_000
    elif m in {"jt", "juta", "m", "mn", "mio", "million"}:
        value *= 1_000_000
    elif m in {"billion", "miliar"}:
        value *= 1_000_000_000
    return value


@dataclass
class SalaryInfo:
    minimum: float | None
    maximum: float | None
    currency: str | None
    period: str | None
    text: str


def _norm_period(p: str | None) -> str | None:
    if not p:
        return None
    p = p.lower()
    if p in {"hour", "hr"}:
        return "hour"
    if p in {"month", "mo", "bulan", "bln"}:
        return "month"
    if p in {"year", "yr", "annum", "annual", "tahun"}:
        return "year"
    return p


def _salary_within_bounds(currency: str | None, lo: float, hi: float | None) -> bool:
    cap = _MAX_SALARY_BY_CURRENCY.get(currency or "", _DEFAULT_MAX_SALARY)
    top = hi if hi is not None else lo
    return 0 < lo <= cap and top <= cap


def parse_salary(text: str | None) -> SalaryInfo | None:
    """Parse an explicit salary statement. Returns None if no currency-qualified amount exists."""
    if not text:
        return None
    for m in _SALARY_RE.finditer(text):
        cur_token, a, b, mult_tail = m.group(1), m.group(2), m.group(3), m.group(4)
        whole = m.group(0)
        # Multiplier written once after the range applies to both ends ("$120-150k").
        inner_mult = re.search(rf"{re.escape(a)}\s?(k|K|rb|ribu|jt|juta|m|M|mn|mio|million|billion|miliar)\b", whole)
        mult_a = inner_mult.group(1) if inner_mult else mult_tail
        lo = _to_number(a, mult_a)
        hi = _to_number(b, mult_tail or mult_a) if b else None
        if lo is None or lo <= 0:
            continue
        if lo < 50 and not (mult_a or mult_tail) and cur_token.lower() not in {"$", "usd", "us$", "€", "eur", "£", "gbp"}:
            continue  # "RM 5" etc. are rarely salaries without a multiplier
        currency = _CURRENCY_SYMBOLS.get(cur_token.lower())
        if hi is not None and hi < lo:
            lo, hi = hi, lo
        if not _salary_within_bounds(currency, lo, hi):
            continue
        ctx = text[max(0, m.start() - 45): min(len(text), m.end() + 45)]
        near_before = text[max(0, m.start() - 25): m.end()]
        if _NON_SALARY_CTX.search(ctx) and not _SALARY_CUE_RE.search(near_before) and not m.group(5):
            continue
        return SalaryInfo(lo, hi, currency, _norm_period(m.group(5)), whole.strip())
    for m in _JUTA_RE.finditer(text):
        ctx = text[max(0, m.start() - 60): min(len(text), m.end() + 60)]
        after = text[m.end(): min(len(text), m.end() + 25)]
        if _NON_SALARY_CTX.search(after) or not _SALARY_CTX_ID.search(ctx):
            continue
        lo = _to_number(m.group(1), "jt")
        hi = _to_number(m.group(2), "jt") if m.group(2) else None
        if lo and _salary_within_bounds("IDR", lo, hi):
            if hi is not None and hi < lo:
                lo, hi = hi, lo
            return SalaryInfo(lo, hi, "IDR", "month", m.group(0).strip())
    return None


def normalise_currency(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[v]
    return value.strip().upper()[:3] if re.fullmatch(r"[A-Za-z]{3}", value.strip()) else None


# ------------------------------------------------------------------ geography


class CountryDetector:
    # Two-letter aliases ("US", "UK") are matched case-sensitively to avoid the word "us".
    _CASE_SENSITIVE_ALIASES = {"us", "uk", "uae"}

    def __init__(self, countries: dict[str, dict[str, Any]]) -> None:
        self._entries: list[tuple[str, list[re.Pattern[str]]]] = []
        self.code_to_name: dict[str, str] = {}
        for name, spec in (countries or {}).items():
            spec = spec or {}
            code = str(spec.get("code", "")).upper()
            self.code_to_name[code] = name
            aliases = {name.lower(), *[str(a).lower() for a in spec.get("aliases", [])]}
            loose = sorted((a for a in aliases if a not in self._CASE_SENSITIVE_ALIASES), key=len, reverse=True)
            pats = [re.compile(r"(?<![\w])(?:" + "|".join(re.escape(a) for a in loose) + r")(?![\w])", re.IGNORECASE)]
            for a in aliases & self._CASE_SENSITIVE_ALIASES:
                pats.append(re.compile(rf"(?<![\w]){a.upper()}(?![\w])"))
            self._entries.append((name, pats))

    def detect(self, *texts: str | None) -> str | None:
        """Country mentioned earliest in the first text that mentions any country."""
        for text in texts:
            if not text:
                continue
            best: tuple[int, str] | None = None
            for name, pats in self._entries:
                for pat in pats:
                    m = pat.search(text)
                    if m and (best is None or m.start() < best[0]):
                        best = (m.start(), name)
            if best:
                return best[1]
        return None

    def detect_all(self, text: str | None) -> list[str]:
        if not text:
            return []
        return [name for name, pats in self._entries if any(p.search(text) for p in pats)]

    def from_code(self, code: str | None) -> str | None:
        if not code:
            return None
        return self.code_to_name.get(code.strip().upper())

    def normalise(self, value: str | None) -> str | None:
        if not value:
            return None
        return self.from_code(value) if len(value.strip()) == 2 else self.detect(value)


@lru_cache(maxsize=4)
def _city_re() -> re.Pattern[str]:
    return re.compile(r"^\s*([A-Za-zÀ-ÿ .'-]{2,40}?)\s*(?:,|\s-\s|$)")


def guess_city(location: str | None, country: str | None) -> str | None:
    """First comma-separated token of a location string when it is not the country itself."""
    if not location:
        return None
    m = _city_re().match(location)
    if not m:
        return None
    city = m.group(1).strip()
    if not city or (country and city.lower() == country.lower()) or city.lower() in {"remote", "anywhere", "worldwide", "hybrid"}:
        return None
    return city


# --------------------------------------------------------------------- titles

_TITLE_NOISE_RE = re.compile(
    r"(?i)\s*(?:[-|–—·]\s*)?(?:job\s+(?:at|in)\s+.*|careers?\s+at\s+.*|\|\s*linkedin.*|-\s*linkedin.*|-\s*jobstreet.*|"
    r"\|\s*glints.*|-\s*indeed.*|\(remote\)|\[remote\])$"
)


def clean_title(title: str | None) -> str:
    if not title:
        return ""
    t = re.sub(r"\s+", " ", title).strip()
    t = _TITLE_NOISE_RE.sub("", t).strip(" -|–—·")
    return t[:200]


JOB_SIGNAL_RE = re.compile(
    r"(?i)\b(hiring|we'?re\s+hiring|we\s+are\s+hiring|now\s+hiring|job\s+opening|open\s+(?:position|role)|vacanc(?:y|ies)|"
    r"apply|lowongan|loker|dibutuhkan|kami\s+mencari|career|careers|recruit(?:ing|ment)|join\s+our\s+team|looking\s+for\s+an?|"
    r"position|role|job|send\s+(?:your\s+)?cv|kirim\s+cv|opportunity)\b"
)
NON_JOB_RE = re.compile(
    r"(?i)\b(salary\s+guide|interview\s+(?:questions|tips)|how\s+to\s+become|resume\s+(?:tips|template)|course|bootcamp|"
    r"tutorial|average\s+salary|gaji\s+rata-rata|top\s+\d+\s+(?:companies|jobs)|statistics|news|webinar|seeking\s+work|"
    r"open\s+to\s+work|looking\s+for\s+(?:a\s+)?(?:job|work|opportunit))"
)


def job_signal_strength(text: str) -> float:
    """0..1 heuristic: does this text read like a job opportunity?"""
    if not text:
        return 0.0
    pos = len(JOB_SIGNAL_RE.findall(text))
    neg = len(NON_JOB_RE.findall(text))
    score = min(1.0, pos / 3.0) - min(0.8, neg * 0.4)
    return max(0.0, score)
