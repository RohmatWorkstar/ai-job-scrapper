"""Application e-mail extraction from job text and ``mailto:`` links.

Only addresses that literally appear in the source (optionally written in the common
``name [at] domain [dot] com`` form) are considered, and only APPLICATION inboxes are kept.

Real postings (checked on 318 live ATS/feed jobs, 2026-11-23) mention many addresses that are not
for applications: disability-accommodation desks, accessibility contacts, "questions?" inboxes,
fraud/scam reporting, support and privacy addresses. Those sentences often contain the word
"application" ("accommodation to complete the application process"), so a keyword nearby is not
enough. An address is kept only when:
* its sentence contains an explicit instruction to send a CV/application ("send your CV to",
  "apply by email", "kirim lamaran ke", ...), or its local part is a recruiting inbox
  (jobs@, careers@, hr@, recruitment@, lamaran@ ...) and the sentence is neutral; and
* its sentence has no non-application context (accommodation, accessibility, disability,
  questions, fraud/scam/report, support, privacy, help/assistance, "applications sent by e-mail
  will not be considered", ...).
"""

from __future__ import annotations

import re
from urllib.parse import unquote

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,24}(?![\w-])"
)
# "jobs [at] acme [dot] com", "jobs (at) acme (dot) com", "jobs{at}acme{dot}com"
_OBF_AT = re.compile(r"\s*[\[\(\{<]\s*(?:at|@)\s*[\]\)\}>]\s*", re.IGNORECASE)
_OBF_DOT = re.compile(r"\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*", re.IGNORECASE)

# Explicit instruction to send an application / CV to the address (EN + ID).
APPLY_INSTRUCTION = re.compile(
    r"(?i)("
    r"send\s+(?:us\s+)?(?:your\s+|an?\s+|the\s+)?(?:updated\s+)?(?:cv|resume|résumé|application|portfolio|cover\s+letter)"
    r"|(?:cv|resume|résumé|applications?|portfolio)\s+(?:should\s+be\s+)?(?:sent\s+)?(?:to|at)\b"
    r"|e-?mail\s+(?:us\s+)?(?:your\s+)?(?:cv|resume|résumé|application|portfolio)"
    r"|apply\s+(?:by|via|through)\s+e-?mail|to\s+apply,?\s+(?:please\s+)?(?:send|e-?mail)"
    r"|apply\s*(?::|by\s+sending|at|to)\s"
    r"|submit\s+(?:your\s+)?(?:cv|resume|résumé|application)\s+(?:to|at|via)"
    r"|interested\??\s+(?:please\s+)?(?:send|e-?mail|reach\s+out|contact)"
    r"|kirim(?:kan)?\s+(?:cv|lamaran|berkas|resume|surat\s+lamaran)"
    r"|(?:lamaran|cv|berkas)\s+(?:anda\s+)?(?:dapat\s+|bisa\s+)?(?:dikirim(?:kan)?\s+)?(?:ke|melalui)\b"
    r"|melamar\s+(?:melalui|via|lewat)\s+e-?mail"
    r")"
)
# Sentences about something other than applying. These always disqualify the address.
NON_APPLICATION = re.compile(
    r"(?i)("
    r"accommodat|accessib|disabilit|reasonable\s+adjustment|alternative\s+(?:method|format)"
    r"|questions?\b|enquir|inquir|fraud|scam|phishing|report\s+(?:it|them|this|any)|suspicious"
    r"|\bsupport\b|privacy|personal\s+data|data\s+protection|gdpr|unsubscribe|\bpress\b|media\s+inquir"
    r"|assistance|need\s+help|technical\s+(?:issue|difficult)|\bfaq\b|having\s+trouble"
    r"|(?:will\s+not|won't|cannot|can't|are\s+not|do\s+not|don't)\s+(?:be\s+)?(?:accept|consider|review|process)"
    r"|not\s+(?:be\s+)?(?:accepted|considered|reviewed)|do\s+not\s+send|bukan\s+untuk\s+lamaran"
    r")"
)
RECRUITING_LOCAL = re.compile(
    r"(?i)^(jobs?|careers?|career\.\w+|recruit\w*|hiring|hire|hr|hrd|hrga|talent|talents|apply|applications?|"
    r"resume|cv|lamaran|loker|rekrutmen|karir|karier|people\.?ops|join(?:us)?|work(?:withus|@)?)([._-]\w+)?$"
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")

MAX_EMAILS = 10


def deobfuscate(text: str) -> str:
    return _OBF_DOT.sub(".", _OBF_AT.sub("@", text))


def _clean(addr: str) -> str:
    return addr.strip().strip(".").lower()


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (or line) containing text[start:end]: at most 300 chars before and 200 after."""
    lo = max(0, start - 300)
    left = lo
    for m in re.finditer(r"[.!?]\s|\n", text[lo:start]):
        left = lo + m.end()
    after = text[end:end + 200]
    m = re.search(r"[.!?](?:\s|$)|\n", after)
    right = end + (m.start() + 1 if m else len(after))
    return text[left:right]


def classify_email_context(sentence: str, address: str) -> str:
    """'application' | 'other' (not an application inbox) | 'unknown'."""
    # Ignore the address itself so "accommodations@..." is judged by its local part below, not the regex.
    rest = sentence.replace(address, " ").replace(address.lower(), " ")
    if NON_APPLICATION.search(rest):
        return "other"
    local = address.split("@", 1)[0].lower()
    if re.search(r"accommodat|accessib|privacy|support|fraud|security|abuse|legal|press|billing|help|info$|noreply|"
                 r"no-reply|askpeople|compliance|dpo|gdpr", local):
        return "other"
    if APPLY_INSTRUCTION.search(rest):
        return "application"
    return "unknown"


def find_emails(*texts: str | None, mailto: list[str] | None = None) -> list[str]:
    """Return application e-mail addresses, best first. Non-application contacts are excluded."""
    best: dict[str, tuple[int, int]] = {}
    excluded: set[str] = set()   # seen in a non-application sentence
    instructed: set[str] = set()  # seen with an explicit "send your CV to ..." instruction
    order = 0

    def consider(addr: str, label: str) -> None:
        nonlocal order
        if label == "other":
            excluded.add(addr)
            return
        local = addr.split("@", 1)[0]
        if label == "application":
            score = 3
            instructed.add(addr)
        elif RECRUITING_LOCAL.match(local):
            score = 2   # neutral sentence, recruiting inbox (jobs@, careers@, hr@ ...)
        else:
            return      # unknown purpose (info@, a person's address with no instruction): not used
        prev = best.get(addr)
        if prev is None or score > prev[0]:
            best[addr] = (score, prev[1] if prev else order)
        order += 1

    for text in texts:
        if not text:
            continue
        plain = deobfuscate(text)
        for m in EMAIL_RE.finditer(plain):
            addr = _clean(m.group(0))
            consider(addr, classify_email_context(_sentence(plain, m.start(), m.end()), m.group(0)))
    for raw in mailto or []:
        target = unquote(raw.split(":", 1)[-1].split("?", 1)[0])
        for a in target.split(","):
            a = a.strip()
            if EMAIL_RE.fullmatch(a):
                addr = _clean(a)
                if addr not in best and addr not in excluded:
                    consider(addr, classify_email_context("", addr))
    # An explicit application instruction wins ("send your CV to hr@x ... questions? hr@x");
    # otherwise any non-application mention disqualifies the address.
    ranked = sorted(((a, s) for a, s in best.items() if a in instructed or a not in excluded),
                    key=lambda kv: (-kv[1][0], kv[1][1]))
    return [a for a, _ in ranked][:MAX_EMAILS]
