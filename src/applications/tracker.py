"""Application tracker: persistent application history in the project's SQLite database.

Status changes are compare-and-set (``UPDATE ... WHERE status = <expected>``) so two
processes can never move the same record twice (for example, send it twice).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from dataclasses import fields
from typing import Any, Iterable

from src.applications.contacts import FREEMAIL_DOMAINS
from src.applications.models import (
    BLOCKING_STATUSES,
    TRANSITIONS,
    ApplicationRecord,
    ApplicationStatus,
    file_sha256,
)
from src.core.exceptions import JobIntelError, StorageError
from src.extraction.job_normalizer import normalise_company
from src.intelligence.deduplication import norm_title
from src.storage.database import Database
from src.utils.date_time import iso, utc_now

_COLUMNS = [f.name for f in fields(ApplicationRecord) if f.name not in ("match", "notes")]


class DuplicateApplicationError(JobIntelError):
    """An application to the same company and position already exists."""

    def __init__(self, message: str, existing: ApplicationRecord) -> None:
        super().__init__(message)
        self.existing = existing


class InvalidTransitionError(JobIntelError):
    """A status change that the workflow does not allow (or a concurrent change won)."""


def make_dedupe_key(company: str | None, title: str | None, recipient: str | None = None) -> str:
    comp = normalise_company(company)
    if not comp and recipient and "@" in recipient:
        domain = recipient.rsplit("@", 1)[1].lower()
        # A free-mail domain says nothing about the employer: use the whole address instead.
        comp = recipient.lower() if domain in FREEMAIL_DOMAINS else "@" + domain
    return f"{comp or '?'}|{norm_title(title or '') or '?'}"


def _now() -> str:
    return iso(utc_now()) or ""


class ApplicationTracker:
    def __init__(self, db: Database, *, recipient_cooldown_days: float = 30) -> None:
        self.db = db
        self.conn = db.conn
        # One application per recipient mailbox: a SENT application blocks the same address for this long.
        self.recipient_cooldown_days = recipient_cooldown_days

    # ------------------------------------------------------------- internals
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        try:
            with self.conn:
                return self.conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise StorageError(f"Database error: {exc}") from exc

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ApplicationRecord:
        d = dict(row)
        kwargs = {k: d.get(k) for k in _COLUMNS}
        kwargs["allow_duplicate"] = bool(d.get("allow_duplicate"))
        kwargs["attempts"] = int(d.get("attempts") or 0)
        kwargs["email_subject"] = d.get("email_subject") or ""
        kwargs["email_body"] = d.get("email_body") or ""
        rec = ApplicationRecord(**kwargs)
        rec.match = json.loads(d.get("match_json") or "{}")
        rec.notes = json.loads(d.get("notes_json") or "[]")
        return rec

    def _event(self, app_id: str, old: str | None, new: str, detail: str = "") -> None:
        self._exec("INSERT INTO application_events(application_id, from_status, to_status, detail, occurred_at) "
                   "VALUES (?,?,?,?,?)", (app_id, old, new, detail[:1000], _now()))

    # ----------------------------------------------------------------- reads
    def get(self, app_id: str) -> ApplicationRecord | None:
        row = self.conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list(self, statuses: Iterable[ApplicationStatus | str] | None = None, limit: int = 200) -> list[ApplicationRecord]:
        if statuses:
            vals = [s.value if isinstance(s, ApplicationStatus) else s for s in statuses]
            rows = self.conn.execute(
                f"SELECT * FROM applications WHERE status IN ({','.join('?' * len(vals))}) ORDER BY created_at LIMIT ?",
                (*vals, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM applications ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_blocking(self, dedupe_key: str, exclude_id: str | None = None,
                      recipient: str | None = None) -> ApplicationRecord | None:
        """Existing application that blocks a new one:
        * same company + position, active or sent (or with an unknown delivery), or
        * same recipient address, active, or sent within ``recipient_cooldown_days``
          (one employer inbox never receives two applications from you)."""
        rows = self.conn.execute("SELECT * FROM applications WHERE dedupe_key=? ORDER BY created_at", (dedupe_key,)).fetchall()
        for r in rows:
            rec = self._row_to_record(r)
            if rec.id == exclude_id:
                continue
            if ApplicationStatus(rec.status) in BLOCKING_STATUSES or rec.error_kind == "delivery_unknown":
                return rec
        if recipient:
            return self.find_recipient_blocking(recipient, exclude_id)
        return None

    def find_recipient_blocking(self, recipient: str, exclude_id: str | None = None) -> ApplicationRecord | None:
        cutoff = iso(utc_now() - timedelta(days=self.recipient_cooldown_days)) or ""
        rows = self.conn.execute("SELECT * FROM applications WHERE lower(recipient_email)=? ORDER BY created_at",
                                 (recipient.strip().lower(),)).fetchall()
        for r in rows:
            rec = self._row_to_record(r)
            if rec.id == exclude_id:
                continue
            status = ApplicationStatus(rec.status)
            if status == ApplicationStatus.SENT:
                if (rec.sent_at or "") >= cutoff:
                    return rec
            elif status in BLOCKING_STATUSES or rec.error_kind == "delivery_unknown":
                return rec
        return None

    def events(self, app_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM application_events WHERE application_id=? ORDER BY id", (app_id,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    # ---------------------------------------------------------------- create
    def create(self, rec: ApplicationRecord | None = None, *, allow_duplicate: bool = False,
               duplicate_authorization: str | None = None, **values: Any) -> ApplicationRecord:
        """Insert a new application. Raises DuplicateApplicationError unless explicitly authorised."""
        if rec is None:
            now = _now()
            rec = ApplicationRecord(id=uuid.uuid4().hex[:12], mode=values.pop("mode"),
                                    status=values.pop("status", ApplicationStatus.NEW.value),
                                    dedupe_key=values.pop("dedupe_key", ""), created_at=now, updated_at=now, **values)
        if not rec.dedupe_key:
            rec.dedupe_key = make_dedupe_key(rec.company_name, rec.job_title, rec.recipient_email)
        existing = self.find_blocking(rec.dedupe_key, recipient=rec.recipient_email)
        if existing is not None:
            if not allow_duplicate:
                same_job = existing.dedupe_key == rec.dedupe_key
                raise DuplicateApplicationError(
                    (f"An application for '{rec.job_title}' at '{rec.company_name or '?'}' already exists "
                     if same_job else
                     f"{rec.recipient_email} already has an application from you ('{existing.job_title}' at "
                     f"'{existing.company_name or '?'}'); one application per recipient ")
                    + f"(id {existing.id}, status {existing.status}).", existing)
            if not duplicate_authorization:
                raise DuplicateApplicationError("A duplicate application needs a recorded authorisation.", existing)
        rec.allow_duplicate = bool(allow_duplicate and existing is not None)
        rec.duplicate_authorization = duplicate_authorization if rec.allow_duplicate else None
        row = {k: getattr(rec, k) for k in _COLUMNS}
        row["allow_duplicate"] = int(rec.allow_duplicate)
        row["match_json"] = json.dumps(rec.match, ensure_ascii=False, default=str)
        row["notes_json"] = json.dumps(rec.notes, ensure_ascii=False)
        cols = list(row)
        self._exec(f"INSERT INTO applications({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [row[c] for c in cols])
        self._event(rec.id, None, rec.status, "created" + (f"; duplicate authorised: {duplicate_authorization}"
                                                           if rec.allow_duplicate else ""))
        return rec

    # ---------------------------------------------------------------- update
    def update_fields(self, app_id: str, **values: Any) -> ApplicationRecord:
        """Update non-status fields. Content changes are handled by :meth:`set_content`."""
        forbidden = {"id", "status", "approval_status", "approved_hash", "approved_at", "approved_by", "send_lock"}
        bad = forbidden & set(values)
        if bad:
            raise ValueError(f"Use the workflow methods to change: {', '.join(sorted(bad))}")
        if "match" in values:
            values["match_json"] = json.dumps(values.pop("match"), ensure_ascii=False, default=str)
        if "notes" in values:
            values["notes_json"] = json.dumps(values.pop("notes"), ensure_ascii=False)
        values["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in values)
        self._exec(f"UPDATE applications SET {sets} WHERE id=?", (*values.values(), app_id))
        rec = self.get(app_id)
        if rec is None:
            raise StorageError(f"Unknown application {app_id}")
        return rec

    def add_note(self, app_id: str, note: str) -> None:
        rec = self.get(app_id)
        if rec is not None:
            self.update_fields(app_id, notes=rec.notes + [note])

    def transition(self, app_id: str, new: ApplicationStatus, detail: str = "", *,
                   expected: ApplicationStatus | None = None, **values: Any) -> ApplicationRecord:
        rec = self.get(app_id)
        if rec is None:
            raise InvalidTransitionError(f"Unknown application {app_id}")
        old = ApplicationStatus(rec.status)
        if expected is not None and old != expected:
            raise InvalidTransitionError(f"Application {app_id} is {old.value}, expected {expected.value}")
        if new not in TRANSITIONS[old]:
            raise InvalidTransitionError(f"Application {app_id}: {old.value} -> {new.value} is not allowed")
        values["updated_at"] = _now()
        sets = ", ".join(["status=?"] + [f"{k}=?" for k in values])
        cur = self._exec(f"UPDATE applications SET {sets} WHERE id=? AND status=?",
                         (new.value, *values.values(), app_id, old.value))
        if cur.rowcount != 1:
            raise InvalidTransitionError(f"Application {app_id} changed concurrently; nothing updated")
        self._event(app_id, old.value, new.value, detail)
        return self.get(app_id)  # type: ignore[return-value]

    # -------------------------------------------------------------- approval
    def set_content(self, app_id: str, *, subject: str | None = None, body: str | None = None,
                    attachment_path: str | None = None, attachment_sha256: str | None = None,
                    generated_cv_path: str | None = None, reason: str = "content changed") -> ApplicationRecord:
        """Change what would be sent. Any earlier approval is withdrawn."""
        rec = self.get(app_id)
        if rec is None:
            raise InvalidTransitionError(f"Unknown application {app_id}")
        if rec.send_lock:
            raise InvalidTransitionError(f"Application {app_id} is being sent (or its delivery is unknown); "
                                         "it cannot be edited now")
        if rec.status not in (ApplicationStatus.PENDING_APPROVAL.value, ApplicationStatus.APPROVED.value,
                              ApplicationStatus.MATCHED.value, ApplicationStatus.NEW.value):
            raise InvalidTransitionError(f"Cannot edit an application in status {rec.status}")
        vals: dict[str, Any] = {}
        if subject is not None:
            vals["email_subject"] = subject
        if body is not None:
            vals["email_body"] = body
        if attachment_path is not None:
            vals["attachment_path"] = attachment_path
            vals["attachment_sha256"] = attachment_sha256
        if generated_cv_path is not None:
            vals["generated_cv_path"] = generated_cv_path
        if rec.status == ApplicationStatus.APPROVED.value:
            self.transition(app_id, ApplicationStatus.PENDING_APPROVAL, f"approval withdrawn: {reason}",
                            approval_status="none", approved_hash=None, approved_at=None, approved_by=None)
        else:
            self._exec("UPDATE applications SET approval_status='none', approved_hash=NULL WHERE id=?", (app_id,))
        return self.update_fields(app_id, **vals) if vals else self.get(app_id)  # type: ignore[return-value]

    def approve(self, app_id: str, approved_by: str = "user") -> ApplicationRecord:
        """Approve exactly what exists now: the attachment is re-hashed at approval time."""
        rec = self.get(app_id)
        if rec is None:
            raise InvalidTransitionError(f"Unknown application {app_id}")
        if rec.attachment_path:
            current = file_sha256(rec.attachment_path)
            if current is None:
                raise InvalidTransitionError(f"Attachment of {app_id} not found: {rec.attachment_path}")
            if current != rec.attachment_sha256:
                self._exec("UPDATE applications SET attachment_sha256=? WHERE id=?", (current, app_id))
                self._event(app_id, rec.status, rec.status, "attachment changed on disk since preparation; re-hashed")
                rec.attachment_sha256 = current
        return self.transition(app_id, ApplicationStatus.APPROVED, f"approved by {approved_by}",
                               expected=ApplicationStatus.PENDING_APPROVAL, approval_status="approved",
                               approved_at=_now(), approved_by=approved_by, approved_hash=rec.content_hash())

    def reject(self, app_id: str, reason: str = "rejected by user") -> ApplicationRecord:
        return self.transition(app_id, ApplicationStatus.REJECTED, reason, approval_status="rejected")

    def skip(self, app_id: str, reason: str = "skipped by user") -> ApplicationRecord:
        return self.transition(app_id, ApplicationStatus.SKIPPED, reason)

    def requeue(self, app_id: str, reason: str = "re-queued by user") -> ApplicationRecord:
        """FAILED (or a stuck send lock) back to PENDING_APPROVAL: the user must approve again."""
        rec = self.get(app_id)
        if rec is None:
            raise InvalidTransitionError(f"Unknown application {app_id}")
        if rec.status == ApplicationStatus.APPROVED.value:
            # Stuck send lock (interrupted send) or content/attachment changed after approval.
            self._exec("UPDATE applications SET send_lock=NULL WHERE id=?", (app_id,))
            return self.transition(app_id, ApplicationStatus.PENDING_APPROVAL, reason, approval_status="none",
                                   approved_hash=None, approved_at=None, approved_by=None)
        # A delivery_unknown failure keeps its send lock; it must be released here or the
        # re-approved record could never be claimed for sending again.
        return self.transition(app_id, ApplicationStatus.PENDING_APPROVAL, reason, expected=ApplicationStatus.FAILED,
                               approval_status="none", approved_hash=None, approved_at=None, approved_by=None,
                               error=None, error_kind=None, send_lock=None)

    # ----------------------------------------------------------------- send
    def claim_send_lock(self, app_id: str) -> bool:
        """Atomically reserve an APPROVED record for one send attempt."""
        cur = self._exec("UPDATE applications SET send_lock=?, attempts=attempts+1 WHERE id=? AND status=? "
                         "AND approval_status='approved' AND send_lock IS NULL",
                         (_now(), app_id, ApplicationStatus.APPROVED.value))
        return cur.rowcount == 1

    def release_send_lock(self, app_id: str) -> None:
        self._exec("UPDATE applications SET send_lock=NULL WHERE id=?", (app_id,))

    def mark_sent(self, app_id: str, *, gmail_message_id: str | None, message_id_header: str, transport: str) -> ApplicationRecord:
        return self.transition(app_id, ApplicationStatus.SENT, f"sent via {transport}", expected=ApplicationStatus.APPROVED,
                               sent_at=_now(), gmail_message_id=gmail_message_id, message_id_header=message_id_header,
                               transport=transport, send_lock=None, error=None, error_kind=None)

    def mark_rehearsed(self, app_id: str, *, detail: str, transport: str = "dry_run") -> ApplicationRecord:
        """After a dry-run / test-recipient send: back to PENDING_APPROVAL, lock released, approval withdrawn."""
        rec = self.transition(app_id, ApplicationStatus.PENDING_APPROVAL, detail, expected=ApplicationStatus.APPROVED,
                              approval_status="none", approved_hash=None, approved_at=None, approved_by=None,
                              send_lock=None, transport=transport)
        self.add_note(app_id, f"Rehearsal done ({detail}). The employer received nothing; approve again in live "
                              "mode to send it for real.")
        return self.get(app_id) or rec

    def mark_failed(self, app_id: str, *, error: str, error_kind: str, transport: str,
                    keep_lock: bool = False) -> ApplicationRecord:
        values: dict[str, Any] = {"error": error[:1000], "error_kind": error_kind, "transport": transport}
        if not keep_lock:
            values["send_lock"] = None
        return self.transition(app_id, ApplicationStatus.FAILED, f"{error_kind}: {error[:300]}", **values)

    # ---------------------------------------------------------- send attempts
    def record_attempt(self, app_id: str | None, outcome: str, transport: str, recipient_domain: str | None,
                       error_kind: str | None = None, error: str | None = None, at: str | None = None) -> None:
        self._exec("INSERT INTO send_attempts(application_id, attempted_at, outcome, transport, recipient_domain, "
                   "error_kind, error) VALUES (?,?,?,?,?,?,?)",
                   (app_id, at or _now(), outcome, transport, recipient_domain, error_kind, (error or "")[:500]))

    def attempts_since(self, since_iso: str, *, outcomes: Iterable[str] | None = None,
                       transport: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM send_attempts WHERE attempted_at >= ?"
        params: list[Any] = [since_iso]
        if outcomes:
            outs = list(outcomes)
            sql += f" AND outcome IN ({','.join('?' * len(outs))})"
            params += outs
        if transport:
            sql += " AND transport = ?"
            params.append(transport)
        rows = self.conn.execute(sql + " ORDER BY attempted_at", params).fetchall()
        return [dict(r) for r in rows]

    def last_attempts(self, n: int, transport: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM send_attempts" + (" WHERE transport=?" if transport else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.conn.execute(sql, ((transport, n) if transport else (n,))).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------- control flags
    def get_control(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sending_control WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_control(self, key: str, value: str | None) -> None:
        if value is None:
            self._exec("DELETE FROM sending_control WHERE key=?", (key,))
        else:
            self._exec("INSERT INTO sending_control(key, value, updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                       (key, value, _now()))
