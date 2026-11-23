"""Sending scheduler: processes the APPROVED queue through the single send gate.

* manual mode     - the review screen calls :meth:`send_one` right after the user approves.
* scheduled mode  - ``python main.py --send-queue`` drains the queue, waiting for the minimum
                    interval / hourly window, and stops at the daily cap, outside the sending
                    window, on a pause, or on the emergency stop. Run it from Windows Task Scheduler
                    (or cron) to spread sends over the day.
* automatic mode  - the same queue processing after policy approval; only when explicitly enabled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.applications.email_service import EmailService, SendRefused
from src.applications.gmail_client import GmailError
from src.applications.models import ApplicationRecord, ApplicationStatus
from src.applications.tracker import ApplicationTracker

_SKIP_RECORD_CODES = {"approval_mismatch", "invalid_recipient", "attachment", "not_approved", "locked",
                      "domain_limit", "invalid_message"}


@dataclass
class QueueReport:
    sent: list[ApplicationRecord] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    stopped_reason: str = ""
    remaining: int = 0


class SendingScheduler:
    def __init__(self, service: EmailService, tracker: ApplicationTracker, *, max_wait_seconds: float = 900,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_event: Callable[[str], None] | None = None) -> None:
        self.service = service
        self.tracker = tracker
        self.max_wait = max_wait_seconds
        self._sleep = sleep
        self._emit = on_event or (lambda msg: None)

    async def send_one(self, app_id: str, *, wait: bool = True) -> tuple[bool, str]:
        """Send one approved application now (waiting for the minimum interval if short)."""
        for _ in range(2):
            try:
                rec = await self.service.send_application(app_id)
                return True, self._describe(rec)
            except SendRefused as exc:
                self._back_to_review(app_id, exc)
                d = exc.decision
                if wait and d is not None and exc.code == "min_interval" and d.wait_seconds <= self.max_wait:
                    self._emit(f"Waiting {int(d.wait_seconds)} s (minimum interval between e-mails)...")
                    await self._sleep(d.wait_seconds + 1)
                    continue
                return False, f"Not sent, kept in the approved queue: {exc}"
            except GmailError as exc:
                return False, f"Gmail error ({exc.kind}): {exc}"
        return False, "Not sent: minimum interval still active; kept in the approved queue."

    @staticmethod
    def _describe(rec: ApplicationRecord) -> str:
        if rec.transport == "dry_run" and rec.status != ApplicationStatus.SENT.value:
            return (f"DRY RUN: e-mail to {rec.recipient_email} written to the outbox; nothing was sent. "
                    "It is back in review: approve it again with dry_run off to send it for real.")
        return f"Sent to {rec.recipient_email} via {rec.transport}."

    def _back_to_review(self, app_id: str, exc: SendRefused) -> None:
        """Content or attachment changed after approval: return it to review instead of leaving it stuck."""
        if exc.code != "approval_mismatch":
            return
        try:
            self.tracker.requeue(app_id, f"returned to review: {exc}")
            self._emit(f"{app_id} changed after approval and was returned to review (python main.py --review).")
        except Exception:  # noqa: BLE001 - best effort; the refusal itself is still reported
            pass

    async def process_queue(self, *, max_sends: int | None = None, wait: bool = True,
                            stop: asyncio.Event | None = None) -> QueueReport:
        report = QueueReport()
        skipped: set[str] = set()
        while True:
            if stop is not None and stop.is_set():
                report.stopped_reason = "stopped by user"
                break
            queue = [r for r in self.tracker.list([ApplicationStatus.APPROVED])
                     if not r.send_lock and r.id not in skipped]
            if not queue:
                break
            rec = queue[0]
            decision = self.service.guard.check(rec.recipient_email)
            if not decision.allowed:
                if decision.code == "domain_limit":
                    skipped.add(rec.id)
                    report.refused.append((rec.id, decision.reason))
                    continue
                if wait and decision.code in ("min_interval", "hourly_limit") and decision.wait_seconds <= self.max_wait:
                    self._emit(f"{decision.reason} Waiting {int(decision.wait_seconds)} s...")
                    await self._sleep(decision.wait_seconds + 1)
                    continue
                report.stopped_reason = decision.reason + (f" Next possible {decision.retry_at_hint}."
                                                           if decision.wait_seconds else "")
                break
            try:
                sent = await self.service.send_application(rec.id)
                report.sent.append(sent)
                self._emit(f"{sent.id}: {sent.job_title} @ {sent.company_name} - {self._describe(sent)}")
            except SendRefused as exc:
                report.refused.append((rec.id, str(exc)))
                self._back_to_review(rec.id, exc)
                if exc.code in _SKIP_RECORD_CODES:
                    skipped.add(rec.id)
                    continue
                report.stopped_reason = str(exc)
                break
            except GmailError as exc:
                report.failed.append((rec.id, f"{exc.kind}: {exc}"))
                self._emit(f"Failed {rec.id}: {exc.kind}")
                if exc.pause_sending or exc.kind == "delivery_unknown":
                    report.stopped_reason = f"Sending stopped after Gmail {exc.kind} error."
                    break
            except Exception as exc:  # noqa: BLE001 - unexpected transport error: stop, never loop on it
                report.failed.append((rec.id, f"unexpected: {exc!r}"))
                report.stopped_reason = f"Sending stopped after an unexpected error: {exc!r}"
                break
            if max_sends is not None and len(report.sent) >= max_sends:
                report.stopped_reason = f"Reached the requested maximum of {max_sends} e-mail(s)."
                break
        report.remaining = len([r for r in self.tracker.list([ApplicationStatus.APPROVED]) if not r.send_lock])
        return report
