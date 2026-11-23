"""Recipient selection and validation for application e-mails.

No network lookups are made (no MX probing, no SMTP "verify"): an address is accepted when it is
syntactically valid, is not a no-reply/system mailbox, does not belong to a job platform or an
example domain, and is not the applicant's own address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.core.models import JobOpportunity
from src.extraction.contact_parser import EMAIL_RE

NO_REPLY = re.compile(r"(?i)^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounce[s]?|"
                      r"notifications?|abuse|privacy|security|unsubscribe|support|billing|press|media|legal|webmaster)@")
PLATFORM_DOMAINS = {
    "linkedin.com", "jobstreet.com", "jobstreet.co.id", "glints.com", "kalibrr.com", "indeed.com", "glassdoor.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workday.com", "myworkday.com",
    "remotive.com", "remoteok.com", "himalayas.app", "jobicy.com", "arbeitnow.com", "wellfound.com",
    "ycombinator.com", "reddit.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "techinasia.com",
    "weworkremotely.com", "dealls.com", "sentry.io", "wixpress.com",
}
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "yourcompany.com",
                   "company.com", "test.com"}
FREEMAIL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.id", "hotmail.com", "outlook.com", "live.com", "ymail.com",
                    "aol.com", "icloud.com", "proton.me", "protonmail.com", "gmx.com", "mail.com"}
_FILE_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|pdf|docx?|js|css)$", re.I)


@dataclass
class RecipientCheck:
    email: str | None
    valid: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


def _domain_in(domain: str, domains: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def validate_address(email: str, *, own_email: str | None = None) -> tuple[bool, str]:
    e = (email or "").strip().lower()
    if not e or len(e) > 254 or not EMAIL_RE.fullmatch(e):
        return False, "not a valid e-mail address"
    local, domain = e.rsplit("@", 1)
    if len(local) > 64 or ".." in e or local.startswith(".") or local.endswith("."):
        return False, "not a valid e-mail address"
    if _FILE_SUFFIX.search(e):
        return False, "looks like a file name, not an address"
    if NO_REPLY.match(e):
        return False, "no-reply or system mailbox"
    if _domain_in(domain, EXAMPLE_DOMAINS):
        return False, "placeholder/example domain"
    if _domain_in(domain, PLATFORM_DOMAINS):
        return False, "belongs to a job platform, not the employer"
    if own_email and e == own_email.strip().lower():
        return False, "this is the applicant's own address"
    return True, ""


def select_recipient(job: JobOpportunity, *, own_email: str | None = None,
                     allow_freemail: bool = True) -> RecipientCheck:
    """Pick the best application address for a job (or explain why there is none)."""
    candidates = list(job.application_emails or [])
    if not candidates:
        return RecipientCheck(None, False, ["No application e-mail address found in the posting "
                                            "(apply through the website/ATS instead)."])
    rejected: list[str] = []
    valid: list[str] = []
    for c in candidates:
        ok, why = validate_address(c, own_email=own_email)
        if ok:
            valid.append(c)
        else:
            rejected.append(f"{c}: {why}")
    if not valid:
        return RecipientCheck(None, False, ["No usable application address."] + rejected, candidates=candidates)
    # Prefer an address on the company's own website domain when one is known.
    site = (job.company_website or "").lower()
    site_domain = re.sub(r"^https?://(www\.)?", "", site).split("/")[0]
    best = next((v for v in valid if site_domain and v.endswith("@" + site_domain)), valid[0])
    check = RecipientCheck(best, True, candidates=candidates)
    domain = best.rsplit("@", 1)[1]
    if domain in FREEMAIL_DOMAINS:
        if not allow_freemail:
            return RecipientCheck(None, False, [f"{best}: free-mail address (disabled by allow_freemail_recipients)"],
                                  candidates=candidates)
        check.warnings.append(f"{best} is a free-mail address; confirm it really belongs to the employer.")
    if len(valid) > 1:
        check.warnings.append(f"Other addresses in the posting: {', '.join(v for v in valid if v != best)}")
    return check
