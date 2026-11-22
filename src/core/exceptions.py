"""Application exception hierarchy.

Exceptions carry a human-readable message suitable for the terminal. They never
include credentials or CV content.
"""

from __future__ import annotations


class JobIntelError(Exception):
    """Base class for all application errors."""


class ConfigurationError(JobIntelError):
    """Invalid or missing configuration."""


class CVError(JobIntelError):
    """CV file missing, unreadable or unsupported."""


class ScannedPDFError(CVError):
    """The PDF contains only images (no extractable text layer)."""


class UnsafeURLError(JobIntelError):
    """URL rejected by the SSRF / scheme validator."""


class FetchBlockedError(JobIntelError):
    """Fetching was not attempted for policy reasons (robots.txt, no-fetch domain, content type)."""


class SourceError(JobIntelError):
    """A discovery source failed. Isolated per source; never fatal to the run."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class AuthenticationError(SourceError):
    """Credentials rejected (HTTP 401/403). Never retried."""

    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=True)


class RateLimitError(SourceError):
    """Provider rate limit reached (HTTP 429) after bounded retries."""


class AIProviderError(JobIntelError):
    """AI provider request failed."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class AIBudgetExceeded(AIProviderError):
    """The configured per-run AI call budget is exhausted."""


class ReportGenerationError(JobIntelError):
    """DOCX/XLSX generation failed."""


class StorageError(JobIntelError):
    """SQLite persistence failed."""
