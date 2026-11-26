"""Anti-fabrication checks for AI-written CV text and e-mails.

A rewrite is rejected (and the original wording kept) when it:
* names a technology the CV does not contain,
* contains a number that is not in its source,
* adds leadership/ownership claims the source does not make,
* drifts too far from the source wording, or grows much longer,
* contains contact details, links or template placeholders.
"""

from __future__ import annotations

import re

from src.extraction.content_parser import SkillDetector

LEADERSHIP = re.compile(r"(?i)\b(led|lead|leading|managed|manage[sd]?|managing|headed|supervis\w*|mentor\w*|"
                        r"architect(?:ed)?|owned|spearhead\w*|direct(?:ed|or)|founded|pioneer\w*|oversaw|"
                        r"chief|principal|expert|world[- ]class)\b")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[a-z][a-z0-9+#.]{3,}")
_PLACEHOLDER = re.compile(r"\[[^\]]{2,40}\]|\{[^}]{2,40}\}|<[A-Z][^>]{1,30}>")
_CONTACT = re.compile(r"(?i)https?://|www\.|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
_STOP = set("with that this from into their there have been were will your about over using used also more "
            "such than them they when where which while within across based role team work".split())


# "one"/"satu" are left out: "one of" / "salah satu" are ordinary phrases, not quantity claims.
_NUMBER_WORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
                 "twenty": "20", "dozen": "12", "hundred": "100", "thousand": "1000", "million": "1000000",
                 "dua": "2", "tiga": "3", "empat": "4", "lima": "5", "enam": "6", "tujuh": "7",
                 "delapan": "8", "sembilan": "9", "sepuluh": "10", "puluhan": "10", "ratusan": "100"}
_NUMBER_WORD_RE = re.compile(r"(?i)\b(" + "|".join(_NUMBER_WORDS) + r")\b")


# Phrases that make an application read as machine-written, weak, desperate, or as a sales pitch.
CLICHES = re.compile(
    r"(?i)(i would love the opportunity|passionate|passion for|thrilled|excited to apply|eager to|"
    r"leverag|synerg|dynamic team|perfect fit|perfect candidate|esteemed|"
    r"proven track record|results-driven|go-getter|hit the ground running|\bdelve|furthermore|moreover|"
    r"in today's|fast-paced|cutting-edge|world-class|i am confident|i'm confident|seamless|"
    r"align(?:s)? perfectly|unique blend|valuable asset|wealth of|testament to|rest assured|"
    r"hope this email finds you well|i would like to introduce myself|just reaching out|reaching out about|"
    r"day-to-day i've worked directly with|please give me a chance|i hope you will consider me|"
    r"waiting for your positive response)")
# Strength claims that need support in the CV text.
OVERCLAIM = re.compile(r"(?i)\b(mainly|primarily|expert|expertise|extensive|extensively|deep knowledge|"
                       r"deep expertise|mastery|highly skilled|seasoned|guru|ninja|rockstar|specialist in)\b")


def check_tone(text: str, cv_text: str) -> list[str]:
    """Clichés, and strength claims the CV itself does not make."""
    problems: list[str] = []
    found = sorted({m.group(0).lower() for m in CLICHES.finditer(text)})
    if found:
        problems.append(f"sounds machine-written/salesy: {', '.join(found)}")
    cv_lower = cv_text.lower()
    claims = sorted({m.group(0).lower() for m in OVERCLAIM.finditer(text)} - {w for w in
                    (m.group(0).lower() for m in OVERCLAIM.finditer(cv_lower))})
    if claims:
        problems.append(f"strength claims not in the CV: {', '.join(claims)}")
    return problems


def numbers_in(text: str) -> set[str]:
    """Digits plus number words ("five years" -> 5), so spelled-out numbers cannot slip through."""
    nums = {n.replace(",", "").rstrip(".") for n in _NUM.findall(text)}
    nums |= {_NUMBER_WORDS[m.group(1).lower()] for m in _NUMBER_WORD_RE.finditer(text)}
    return nums


def _words(text: str) -> list[str]:
    return [w.strip(".") for w in _WORD.findall(text.lower()) if w.strip(".") not in _STOP]


def unsupported_technologies(text: str, cv_text: str, detector: SkillDetector, allowed_text: str = "") -> list[str]:
    have = {s.lower() for s in detector.detect_names(cv_text)} | {s.lower() for s in detector.detect_names(allowed_text)}
    return [s for s in detector.detect_names(text) if s.lower() not in have]


def check_rewrite(new: str, source: str, cv_text: str, detector: SkillDetector, *,
                  min_overlap: float = 0.5, max_growth: float = 1.6) -> list[str]:
    """Problems with a rephrased CV line (empty list = acceptable)."""
    problems: list[str] = []
    new = new.strip()
    if not new:
        return ["empty"]
    techs = unsupported_technologies(new, cv_text, detector)
    if techs:
        problems.append(f"introduces technologies not in the CV: {', '.join(techs)}")
    extra_nums = numbers_in(new) - numbers_in(source)
    if extra_nums:
        problems.append(f"introduces numbers not in the original: {', '.join(sorted(extra_nums))}")
    claims = {m.group(0).lower() for m in LEADERSHIP.finditer(new)} - {m.group(0).lower() for m in LEADERSHIP.finditer(source)}
    if claims:
        problems.append(f"adds claims not in the original: {', '.join(sorted(claims))}")
    words = _words(new)
    if words:
        known = set(_words(source)) | set(_words(cv_text))  # whole words, not substrings
        supported = sum(1 for w in words if w in known)
        if supported / len(words) < min_overlap:
            problems.append("wording drifts too far from the CV")
    if len(new) > len(source) * max_growth + 40:
        problems.append("much longer than the original")
    if _CONTACT.search(new) or _PLACEHOLDER.search(new):
        problems.append("contains links, e-mail addresses or placeholders")
    return problems


def check_free_text(text: str, cv_text: str, detector: SkillDetector, *, allowed_text: str = "",
                    allowed_numbers: set[str] | None = None, min_overlap: float = 0.0) -> list[str]:
    """Problems with an AI-written summary or e-mail body."""
    problems: list[str] = []
    techs = unsupported_technologies(text, cv_text, detector, allowed_text)
    if techs:
        problems.append(f"mentions technologies not in the CV: {', '.join(techs)}")
    extra = numbers_in(text) - numbers_in(cv_text) - numbers_in(allowed_text) - (allowed_numbers or set())
    if extra:
        problems.append(f"mentions numbers not in the CV: {', '.join(sorted(extra))}")
    claims = {m.group(0).lower() for m in LEADERSHIP.finditer(text)} - \
        {m.group(0).lower() for m in LEADERSHIP.finditer(cv_text + " " + allowed_text)}
    if claims:
        problems.append(f"makes claims not in the CV: {', '.join(sorted(claims))}")
    if _CONTACT.search(text) or _PLACEHOLDER.search(text):
        problems.append("contains links, e-mail addresses or placeholders")
    problems += check_tone(text, cv_text)
    if min_overlap:
        words = _words(text)
        base = set(_words(cv_text + " " + allowed_text))
        if words and sum(1 for w in words if w in base) / len(words) < min_overlap:
            problems.append("wording is not grounded in the CV")
    return problems
