"""Application e-mail extraction from job text and ``mailto:`` links.

Only addresses that literally appear in the source (optionally written in the common
``name [at] domain [dot] com`` form) are returned. Addresses next to "apply / send your CV /
kirim lamaran" wording are ranked first, because they are most likely the application inbox.
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

APPLY_CONTEXT = re.compile(
    r"(?i)(apply|application|send\s+(?:your\s+)?(?:cv|resume|résumé|portfolio)|cv\s+to|resume\s+to|email\s+us|"
    r"e-?mail\s+(?:your|to)|submit|interested|kirim(?:kan)?|lamaran|melamar|daftar|cv\s+ke|hrd|recruit|career|talent|hiring)"
)

MAX_EMAILS = 10


def deobfuscate(text: str) -> str:
    return _OBF_DOT.sub(".", _OBF_AT.sub("@", text))


def _clean(addr: str) -> str:
    return addr.strip().strip(".").lower()


def find_emails(*texts: str | None, mailto: list[str] | None = None) -> list[str]:
    """Return unique e-mail addresses ranked by "application" context (best first)."""
    scored: dict[str, tuple[int, int]] = {}
    order = 0
    for raw in mailto or []:
        addr = unquote(raw.split(":", 1)[-1].split("?", 1)[0])
        for a in addr.split(","):
            if EMAIL_RE.fullmatch(a.strip()):
                # No automatic priority: a mailto link ranks by the same "apply" context as text
                # addresses (its visible text usually repeats the address with that context).
                scored.setdefault(_clean(a), (1, order))
                order += 1
    for text in texts:
        if not text:
            continue
        plain = deobfuscate(text)
        for m in EMAIL_RE.finditer(plain):
            addr = _clean(m.group(0))
            # Context = the same line only (120 chars before, 40 after), so a neighbouring
            # "send your CV" line does not promote an unrelated address.
            line_start = plain.rfind("\n", 0, m.start()) + 1
            line_end = plain.find("\n", m.end())
            line_end = len(plain) if line_end == -1 else line_end
            window = plain[max(line_start, m.start() - 120): min(line_end, m.end() + 40)]
            score = 2 if APPLY_CONTEXT.search(window) else 1
            prev = scored.get(addr)
            if prev is None or score > prev[0]:
                scored[addr] = (score, prev[1] if prev else order)
            order += 1
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [a for a, _ in ranked][:MAX_EMAILS]
