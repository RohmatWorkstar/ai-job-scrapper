"""Relevance gating for high-volume structured feeds (ATS boards, job APIs).

ATS boards can list hundreds of unrelated roles (sales, legal, ...). The prefilter
keeps postings whose *title* plausibly matches the candidate's role families or
skills, and drops postings whose *known* publication date is outside the selected
window. Unknown dates are always kept. Counts of scanned vs kept items are reported
per source, so nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from src.core.configuration import Settings
from src.core.models import SearchConfig
from src.cv.models import CandidateProfile
from src.utils.date_time import within_window

GENERIC_TECH_TITLE = re.compile(r"(?i)\b(engineer|developer|programmer|software|swe|sde|coder|technologist|pengembang)\b")
CORE_SWE_TITLE = re.compile(
    r"(?i)\b(software|back[\s-]?end|front[\s-]?end|full[\s-]?stack|devops|sre|site\s+reliability|"
    r"data\s+engineer|ml\s+engineer|ai\s+engineer|mobile\s+engineer|web\s+developer|platform\s+engineer|"
    r"systems?\s+engineer|cloud\s+engineer|qa\s+engineer|security\s+engineer)\b"
)
NON_TECH_TITLE = re.compile(
    r"(?i)\b(sales|account\s+(?:executive|manager)|recruit(?:er|ing)|talent|marketing|legal|counsel|finance|"
    r"accountant|accounting|nurse|driver|warehouse|customer\s+success|support\s+specialist|hr\b|people\s+partner|"
    r"payroll|procurement|copywriter|teacher|chef|cashier|mechanical|civil|electrical|hardware|chemical|"
    r"pilates|instructor|solutions?\s+engineer|sales\s+engineer|gtm|go[\s-]to[\s-]market|client\s+partner|"
    r"partnerships|business\s+development|operations\s+manager|strategy\s+and\s+operations|"
    r"certification\s+assessment|field\s+marketing|revenue\s+operations|deal\s+desk)\b"
)


@dataclass
class PrefilterDecision:
    keep: bool
    reason: str


class Prefilter:
    def __init__(self, settings: Settings, profile: CandidateProfile, config: SearchConfig) -> None:
        fams = settings.profiles.get("role_families") or {}
        kws: set[str] = set()
        for fam in profile.role_families or []:
            if fam == "software_engineering":
                continue
            kws.update(k.lower() for k in fams.get(fam, {}).get("keywords", []))
        for title in (config.target_positions or []) + profile.target_positions:
            core = re.sub(r"(?i)\b(senior|junior|lead|sr\.?|jr\.?|mid|staff|principal)\b", "", title).strip().lower()
            core = re.sub(r"\s+", " ", core)
            if core and core not in {"software engineer", "software developer", "engineer", "developer"}:
                kws.add(core)
            first = core.split(" ")[0] if core else ""
            if first and first not in {"software", "engineer", "developer", "senior", "it", "web", "programmer"} and len(first) > 2:
                kws.add(first)
        self.role_keywords = sorted(kws, key=len, reverse=True)
        self.skills = sorted({s.lower() for s in profile.skills if len(s) > 1}, key=len, reverse=True)
        self.window = config.publication_window_days
        self._role_re = (
            re.compile(r"(?<![\w])(?:" + "|".join(re.escape(k) for k in self.role_keywords) + r")(?![\w])", re.I)
            if self.role_keywords else None
        )
        self._skill_re = (
            re.compile(r"(?<![\w+#.])(?:" + "|".join(re.escape(s) for s in self.skills) + r")(?![\w+#])", re.I)
            if self.skills else None
        )
        self.wants_generic = "software_engineering" in (profile.role_families or []) or not self.role_keywords

    def title_relevant(self, title: str, tags: list[str] | None = None, text: str = "") -> tuple[bool, str]:
        t = title or ""
        if NON_TECH_TITLE.search(t) and not CORE_SWE_TITLE.search(t):
            return False, "non-matching job family"
        if self._role_re and self._role_re.search(t):
            return True, "title matches target role"
        if self._skill_re and self._skill_re.search(t + " " + " ".join(tags or [])):
            return True, "title/tags mention a candidate skill"
        if self.wants_generic and GENERIC_TECH_TITLE.search(t):
            if not text or not self._skill_re:
                return True, "generic software title"
            hits = {m.group(0).lower() for m in self._skill_re.finditer(text[:4000])}
            if len(hits) >= 2:
                return True, "generic software title with matching skills in description"
            return False, "generic title without matching skills"
        return False, "title not related to candidate roles"

    def date_ok(self, published: datetime | None) -> bool:
        return within_window(published, self.window) is not False

    def decide(self, title: str, tags: list[str] | None, text: str, published: datetime | None) -> PrefilterDecision:
        if not self.date_ok(published):
            return PrefilterDecision(False, "outside publication window")
        keep, reason = self.title_relevant(title, tags, text)
        return PrefilterDecision(keep, reason)
