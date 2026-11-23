"""E-mail service: MIME building, attachment validation, transports and the send gate.

``EmailService.send_application`` is the ONLY path that sends anything. It refuses to send unless:
1. the record is APPROVED and the approval hash equals the hash of the content about to be sent,
2. the sending guard allows it (emergency stop, pause, window, daily/hourly/interval/domain caps),
3. the recipient and the PDF attachment validate,
4. the record's send lock can be claimed atomically (no double sends).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from src.applications.contacts import validate_address
from src.applications.gmail_client import DeliveryUnknownError, GmailAuthError, GmailClient, GmailError

AUTH_PAUSE_PREFIX = "Gmail authorisation needed (run --gmail-auth)"
from src.applications.models import ApplicationRecord, ApplicationStatus, file_sha256
from src.applications.sending_limits import Decision, SendingGuard
from src.applications.tracker import ApplicationTracker
from src.core.exceptions import JobIntelError
from src.utils.date_time import local_now
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)


class SendRefused(JobIntelError):
    """Sending was not attempted (not approved, blocked by a limit, invalid data)."""

    def __init__(self, message: str, *, code: str = "refused", decision: Decision | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.decision = decision


class AttachmentError(JobIntelError):
    """The CV attachment is missing, not a valid PDF, or too large."""


def validate_attachment(path: str | Path | None, max_bytes: int = 10 * 1024 * 1024) -> Path:
    if not path:
        raise AttachmentError("No attachment set.")
    p = Path(path)
    if not p.is_file():
        raise AttachmentError(f"Attachment not found: {p}")
    if p.suffix.lower() != ".pdf":
        raise AttachmentError(f"Attachment must be a PDF: {p.name}")
    size = p.stat().st_size
    if size == 0 or size > max_bytes:
        raise AttachmentError(f"Attachment size {size} bytes is outside 1..{max_bytes} bytes: {p.name}")
    data = p.read_bytes()
    if not data.startswith(b"%PDF"):
        raise AttachmentError(f"Attachment is not a valid PDF file: {p.name}")
    try:
        import pymupdf

        doc = pymupdf.open("pdf", data)
        try:
            if doc.needs_pass or doc.is_encrypted:
                raise AttachmentError(f"Attachment is password-protected: {p.name}")
            if doc.page_count < 1:
                raise AttachmentError(f"Attachment has no pages: {p.name}")
        finally:
            doc.close()
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AttachmentError(f"Attachment cannot be opened as PDF ({exc}): {p.name}") from exc
    return p


def attachment_name(record: ApplicationRecord, identity_name: str = "") -> str:
    base = Path(record.attachment_path or "CV.pdf").name
    if record.mode == "basic" and identity_name:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", identity_name).strip("_")
        return f"{safe}_CV.pdf" if safe else base
    return base


def build_message(*, sender: str, sender_name: str, recipient: str, subject: str, body: str,
                  attachment: Path, attachment_filename: str, reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    msg.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment_filename)
    return msg


# ------------------------------------------------------------- transports
@dataclass
class TransportResult:
    message_id: str | None
    transport: str


class MailTransport(Protocol):
    name: str

    async def send(self, msg: EmailMessage) -> TransportResult: ...


class DryRunTransport:
    """Writes .eml files to an outbox folder. Never contacts any mail server."""

    name = "dry_run"
    rehearsal = True  # the send gate keeps the application sendable for real afterwards

    def __init__(self, outbox: Path) -> None:
        self.outbox = outbox

    async def send(self, msg: EmailMessage) -> TransportResult:
        self.outbox.mkdir(parents=True, exist_ok=True)
        stamp = local_now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.outbox / f"{stamp}.eml"
        path.write_bytes(msg.as_bytes())
        return TransportResult(message_id=f"dry-run:{path.name}", transport=self.name)


class GmailTransport:
    name = "gmail"

    def __init__(self, client: GmailClient) -> None:
        self.client = client

    async def send(self, msg: EmailMessage) -> TransportResult:
        return TransportResult(message_id=await self.client.send_raw(msg.as_bytes()), transport=self.name)


# ------------------------------------------------------------------ service
class EmailService:
    def __init__(self, tracker: ApplicationTracker, guard: SendingGuard, transport: MailTransport, *,
                 sender: str, sender_name: str = "", max_attachment_bytes: int = 10 * 1024 * 1024,
                 own_email: str | None = None) -> None:
        if not sender or "@" not in sender:
            raise JobIntelError("A sender address is required (GMAIL_SENDER_ADDRESS in .env).")
        if guard.transport_name != transport.name:
            raise JobIntelError("Sending guard and transport must use the same counters")
        self.tracker = tracker
        self.guard = guard
        self.transport = transport
        self.sender = sender
        self.sender_name = sender_name
        self.max_attachment_bytes = max_attachment_bytes
        self.own_email = own_email

    def preflight(self, rec: ApplicationRecord) -> None:
        """Checks that do not change any state. Raises SendRefused."""
        if rec.status != ApplicationStatus.APPROVED.value or rec.approval_status != "approved":
            raise SendRefused(f"Application {rec.id} is {rec.status}; only APPROVED applications can be sent.",
                              code="not_approved")
        if rec.send_lock:
            raise SendRefused(f"Application {rec.id} already has a send in progress or an unknown delivery result "
                              "(check the Gmail Sent folder, then use --requeue).", code="locked")
        current_sha = file_sha256(rec.attachment_path)
        if current_sha != rec.attachment_sha256:
            raise SendRefused(f"The attachment of {rec.id} changed after approval; review it again.",
                              code="approval_mismatch")
        if rec.content_hash() != rec.approved_hash:
            raise SendRefused(f"The content of {rec.id} changed after approval; review it again.",
                              code="approval_mismatch")
        ok, why = validate_address(rec.recipient_email or "", own_email=self.own_email)
        if not ok:
            raise SendRefused(f"Recipient of {rec.id} is invalid: {why}", code="invalid_recipient")
        try:
            validate_attachment(rec.attachment_path, self.max_attachment_bytes)
        except AttachmentError as exc:
            raise SendRefused(str(exc), code="attachment") from exc

    async def send_application(self, app_id: str) -> ApplicationRecord:
        rec = self.tracker.get(app_id)
        if rec is None:
            raise SendRefused(f"Unknown application {app_id}", code="unknown")
        self.preflight(rec)
        decision = self.guard.check(rec.recipient_email)
        if not decision.allowed:
            raise SendRefused(decision.reason, code=decision.code, decision=decision)
        # Build the message before taking the lock: a failure here must not leave a stuck lock.
        try:
            msg = build_message(sender=self.sender, sender_name=self.sender_name, recipient=rec.recipient_email or "",
                                subject=rec.email_subject, body=rec.email_body,
                                attachment=Path(rec.attachment_path or ""),
                                attachment_filename=attachment_name(rec, self.sender_name))
        except (OSError, ValueError) as exc:
            raise SendRefused(f"Could not build the e-mail for {rec.id}: {exc}", code="invalid_message") from exc
        if not self.tracker.claim_send_lock(rec.id):
            raise SendRefused(f"Application {rec.id} could not be locked for sending (changed concurrently).",
                              code="locked")
        message_id = str(msg["Message-ID"])
        try:
            result = await self.transport.send(msg)
        except GmailAuthError as exc:
            # Raised before or instead of delivery (token refresh failed / credentials rejected):
            # nothing was sent, so the application stays APPROVED and is not counted as an attempt.
            self.tracker.release_send_lock(rec.id)
            self.guard.pause(f"{AUTH_PAUSE_PREFIX}: {str(exc)[:200]}")
            raise
        except DeliveryUnknownError as exc:
            self.guard.record(rec.id, "failed", rec.recipient_email, error_kind=exc.kind, error=str(exc))
            self.tracker.mark_failed(rec.id, error=str(exc), error_kind=exc.kind, transport=self.transport.name,
                                     keep_lock=True)
            self.tracker.update_fields(rec.id, message_id_header=message_id)
            raise
        except GmailError as exc:
            kind = "invalid_recipient" if exc.kind == "invalid_request" and \
                re.search(r"(?i)recipient|to header|invalid address", str(exc)) else exc.kind
            outcome = "blocked" if exc.pause_sending else "failed"
            self.guard.record(rec.id, outcome, rec.recipient_email, error_kind=kind, error=str(exc))
            if exc.pause_sending:
                hours = self.guard.limits.quota_pause_hours if exc.kind in ("sending_limit", "quota") else None
                self.guard.pause(f"Gmail {exc.kind} error: {str(exc)[:200]}", hours=hours)
            self.tracker.mark_failed(rec.id, error=str(exc), error_kind=kind, transport=self.transport.name)
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected transport failure: treat as unknown delivery
            self.guard.record(rec.id, "failed", rec.recipient_email, error_kind="unexpected", error=repr(exc))
            self.tracker.mark_failed(rec.id, error=f"Unexpected error: {exc!r}", error_kind="delivery_unknown",
                                     transport=self.transport.name, keep_lock=True)
            raise
        self.guard.record(rec.id, "sent", rec.recipient_email)
        log_event(log, 20, "application.sent", id=rec.id, transport=result.transport)
        if getattr(self.transport, "rehearsal", False):
            # A dry run is a rehearsal: the record must stay sendable for real, and a live send needs
            # a fresh approval (made while dry_run is off), so it goes back to PENDING_APPROVAL.
            return self.tracker.mark_rehearsed(rec.id, detail=f"dry run: written to {result.message_id}")
        return self.tracker.mark_sent(rec.id, gmail_message_id=result.message_id, message_id_header=message_id,
                                      transport=result.transport)
