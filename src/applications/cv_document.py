"""Backward-compatible re-export facade for :mod:`src.cv.document`."""

from __future__ import annotations

from src.cv.document import (
    _BULLET,
    _BULLET_CHARS,
    CVDocument,
    ExperienceEntry,
    _default_detector,
    _items,
    _nice_name,
    _split_outside_parens,
    _split_title_company,
    _verbatim,
    _verbatim_norm,
    display_link,
    extract_identity,
    parse_experience,
    parse_skills,
    structure_cv,
    structure_cv_ai,
)

__all__ = [
    "CVDocument",
    "ExperienceEntry",
    "display_link",
    "extract_identity",
    "parse_experience",
    "parse_skills",
    "structure_cv",
    "structure_cv_ai",
]
