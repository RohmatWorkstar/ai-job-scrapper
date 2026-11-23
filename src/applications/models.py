"""Application records, statuses and shared value objects."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ApplicationStatus(str, Enum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    MATCHED = "MATCHED"
    REJECTED = "REJECTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


S = ApplicationStatus

# Allowed status transitions. Anything else is refused by the tracker.
TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    S.NEW: {S.ANALYZED, S.PENDING_APPROVAL, S.REJECTED, S.SKIPPED, S.FAILED},
    S.ANALYZED: {S.MATCHED, S.REJECTED, S.SKIPPED, S.FAILED},
    S.MATCHED: {S.PENDING_APPROVAL, S.REJECTED, S.SKIPPED, S.FAILED},
    S.PENDING_APPROVAL: {S.PENDING_APPROVAL, S.APPROVED, S.REJECTED, S.SKIPPED},
    # APPROVED -> PENDING_APPROVAL happens when the content changes after approval.
    S.APPROVED: {S.SENT, S.FAILED, S.PENDING_APPROVAL, S.SKIPPED},
    # A failed application is only sent again after the user re-approves it.
    S.FAILED: {S.PENDING_APPROVAL, S.SKIPPED},
    S.SENT: set(),
    S.REJECTED: set(),
    S.SKIPPED: set(),
}

# Records in these states block a second application to the same company + position.
BLOCKING_STATUSES = {S.NEW, S.ANALYZED, S.MATCHED, S.PENDING_APPROVAL, S.APPROVED, S.SENT}


class ApplicationMode(str, Enum):
    BASIC = "basic"
    AI = "ai"


@dataclass
class CandidateIdentity:
    """Contact details printed on generated CVs and e-mail signatures. Never sent to the AI provider."""

    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplicationRecord:
    id: str
    mode: str
    status: str
    dedupe_key: str
    created_at: str
    updated_at: str
    job_key: str | None = None
    session_id: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    job_url: str | None = None
    recipient_email: str | None = None
    match_score: float | None = None
    match: dict[str, Any] = field(default_factory=dict)
    original_cv_path: str | None = None
    original_cv_sha256: str | None = None
    generated_cv_path: str | None = None
    attachment_path: str | None = None
    attachment_sha256: str | None = None
    email_subject: str = ""
    email_body: str = ""
    approval_status: str = "none"         # none | approved | rejected
    approved_at: str | None = None
    approved_by: str | None = None
    approved_hash: str | None = None
    allow_duplicate: bool = False
    duplicate_authorization: str | None = None
    send_lock: str | None = None
    attempts: int = 0
    error: str | None = None
    error_kind: str | None = None
    gmail_message_id: str | None = None
    message_id_header: str | None = None
    transport: str | None = None
    notes: list[str] = field(default_factory=list)
    sent_at: str | None = None

    @property
    def status_enum(self) -> ApplicationStatus:
        return ApplicationStatus(self.status)

    def content_hash(self) -> str:
        """Hash of everything the recipient would receive. Approval is bound to this value."""
        parts = [self.recipient_email or "", self.email_subject or "", self.email_body or "",
                 self.attachment_path or "", self.attachment_sha256 or ""]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def file_sha256(path: str | None) -> str | None:
    if not path:
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()
