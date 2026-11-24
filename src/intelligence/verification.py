"""Evidence-based verification status assignment.

A successful HTTP response alone never yields VERIFIED_OPEN. VERIFIED_OPEN requires
the posting to be present on the company's own recruiting channel at retrieval
time (live ATS listing, or a company-owned page carrying structured JobPosting
data with no closing markers and a non-expired validThrough). Third-party
listings top out at LIKELY_OPEN; social/community posts need human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.configuration import Settings
from src.core.models import ContentKind, JobOpportunity, VerificationStatus
from src.extraction.content_parser import job_signal_strength
from src.intelligence.evidence import add_evidence
from src.utils.date_time import age_days, format_local, from_iso, utc_now

FREE_MAIL = ("@gmail.com", "@yahoo.", "@hotmail.", "@outlook.com", "@ymail.com")
NEGATION_RE = re.compile(
    r"(?i)\b(never|not|no|won'?t|will\s+not|do\s+not|don'?t|does\s+not|doesn'?t|without|tidak|tanpa|"
    r"tidak\s+pernah|bebas|free\s+of)\b|\bbeware\b|\bscam|\bfraud")


@dataclass
class VerificationResult:
    status: VerificationStatus
    reason: str


class Verifier:
    def __init__(self, settings: Settings) -> None:
        self.scam_indicators = [s.lower() for s in settings.get("verification.scam_indicators", []) or []]
        self.tz = settings.timezone

    def scam_signals(self, job: JobOpportunity) -> list[str]:
        """Indicators present in an affirmative context. Anti-fraud disclaimers such as
        "we will never ask you to pay a fee" are ignored."""
        text = f"{job.job_title}\n{job.job_description}".lower()
        hits: list[str] = []
        for s in self.scam_indicators:
            start = 0
            while (idx := text.find(s, start)) >= 0:
                start = idx + len(s)
                if not NEGATION_RE.search(text[max(0, idx - 50): idx]):
                    hits.append(s)
                    break
        if any(m in text for m in FREE_MAIL) and re.search(
                r"\b(?:registration|training|admin(?:istration)?)\s+fee\b|"
                r"\bbiaya\s+(?:pendaftaran|administrasi|pelatihan|tes|test|interview|seragam|materai|jaminan|tiket|akomodasi|deposit)\b",
                text) and not NEGATION_RE.search(text):
            hits.append("free-mail contact combined with payment wording")
        return hits

    def verify(self, job: JobOpportunity) -> VerificationResult:
        fetch = job.signals.get("fetch") or {}
        retrieved = format_local(job.discovered_at, self.tz)
        notes: list[str] = []

        scam = self.scam_signals(job)
        if scam:
            return VerificationResult(VerificationStatus.POSSIBLE_SCAM,
                                      "Contains warning signs: " + ", ".join(sorted(set(scam))[:5]) +
                                      ". Never pay fees to apply; verify the company independently.")

        if fetch and not fetch.get("ok"):
            kind, status = fetch.get("error_kind"), fetch.get("status")
            if status in (404, 410):
                return VerificationResult(VerificationStatus.INACCESSIBLE,
                                          f"Original page returned HTTP {status} when checked ({retrieved}); "
                                          "the listing may have been removed or closed.")
            if kind in ("network", "timeout") or (status and status >= 500):
                return VerificationResult(VerificationStatus.INACCESSIBLE,
                                          f"Original page could not be opened ({fetch.get('error')}) when checked.")
            if kind in ("blocked_robots", "policy") or status in (401, 403, 429):
                notes.append("page not fetched for verification (" + (fetch.get("error") or f"HTTP {status}") + ")")

        markers = fetch.get("closed_markers") or []
        if fetch.get("ok") and markers:
            return VerificationResult(VerificationStatus.CLOSED,
                                      f"The page states the position is closed (\"{markers[0]}\").")
        vt = from_iso(fetch.get("valid_through")) if fetch.get("valid_through") else None
        if vt and vt < utc_now():
            return VerificationResult(VerificationStatus.CLOSED,
                                      f"Structured data validThrough date ({format_local(vt, self.tz, '%Y-%m-%d')}) has passed.")

        if job.signals.get("not_a_job"):
            return VerificationResult(VerificationStatus.NEEDS_REVIEW,
                                      "The content may not be a job opening (article, job-seeker post or other).")

        company_known = bool(job.company_name)
        if job.listed_in_live_feed and job.official_channel:
            if company_known:
                return VerificationResult(VerificationStatus.VERIFIED_OPEN,
                                          f"Listed on the company's own applicant-tracking job board ({job.source_name}) "
                                          f"in the live API response at {retrieved}. Applications go through the "
                                          "company's official recruiting system.")
            return VerificationResult(VerificationStatus.LIKELY_OPEN,
                                      f"Listed on a live ATS board ({job.source_name}) but the company name is unclear.")

        if job.page_fetched and fetch.get("has_job_posting_ld"):
            apply = bool(job.application_url or fetch.get("apply_links"))
            if job.official_channel and company_known and apply:
                return VerificationResult(VerificationStatus.VERIFIED_OPEN,
                                          f"Company-owned page checked at {retrieved}: structured JobPosting data present, "
                                          "no closing notice, application link available.")
            return VerificationResult(VerificationStatus.LIKELY_OPEN,
                                      f"Page checked at {retrieved} carries structured JobPosting data and no closing "
                                      "notice" + ("" if job.official_channel else " (third-party site)") + ".")

        if job.listed_in_live_feed:
            return VerificationResult(VerificationStatus.LIKELY_OPEN,
                                      f"Currently listed in the {job.source_name} job feed (third-party aggregator) at "
                                      f"{retrieved}; not confirmed on the company's own channel.")

        if job.content_kind in (ContentKind.SOCIAL_POST.value, ContentKind.COMMUNITY_POST.value):
            return VerificationResult(VerificationStatus.NEEDS_REVIEW,
                                      "Public post by a third party (recruiter, employee or community member). The "
                                      "company's authorization and whether applications are still accepted are not "
                                      "confirmed." + (" " + "; ".join(notes) if notes else ""))

        signal = job.signals.get("job_signal", job_signal_strength(f"{job.job_title} {job.job_description[:1500]}"))
        if job.page_fetched:
            age = age_days(job.published_at)
            if signal >= 0.6 and (job.application_url or fetch.get("apply_links")) and age is not None and age <= 30:
                return VerificationResult(VerificationStatus.LIKELY_OPEN,
                                          "Page opened, contains a recent job advertisement and an application link, "
                                          "but no structured posting data to confirm status.")
            return VerificationResult(VerificationStatus.UNVERIFIED,
                                      "Page opened but its content does not confirm that applications are open.")
        reason = "Known only from a search-engine result; the original page was not checked."
        if notes:
            reason += " " + "; ".join(notes).capitalize() + "."
        return VerificationResult(VerificationStatus.UNVERIFIED, reason)

    def apply(self, job: JobOpportunity) -> None:
        res = self.verify(job)
        job.verification_status = res.status.value
        job.verification_reason = res.reason
        add_evidence(job, "verification", res.status.value, res.reason, "http_check")
        if job.signals.get("stale"):
            job.warnings.append("Posting is older than 60 days.")
