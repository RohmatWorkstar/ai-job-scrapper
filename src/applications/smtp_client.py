"""Gmail over SMTP with an App Password (alternative when no Google Cloud project is available).

* Server: smtp.gmail.com:587 with STARTTLS (certificate verified), or 465 with implicit TLS.
* Credentials: GMAIL_SENDER_ADDRESS + GMAIL_APP_PASSWORD from .env only. The App Password is
  registered with the log redactor and never printed. Unlike the gmail.send OAuth scope, an App
  Password grants full account access, so the OAuth transport remains the preferred option.
* Gmail's sending limits are shared by web, SMTP and API clients, so this transport uses the same
  "gmail" counters as the API transport.

Errors are mapped to the same classes as the Gmail API client, so the send gate, auto-pause and
"never retry a blocked e-mail" rules apply unchanged:
* 535/534/530 (credentials)                  -> GmailAuthError (sending pauses; nothing was sent)
* 550 5.4.5 / "sending limit"/"quota"        -> GmailSendingLimitError (pause 24 h, never retried)
* 5xx 5.7.x (policy/spam)                    -> GmailPolicyError (pause until manual resume)
* 4xx 4.7.x (rate limiting)                  -> GmailQuotaError (pause, never retried)
* 550/553 5.1.x, refused recipients          -> GmailRequestError "invalid recipient" (not retried)
* other 4xx before the message was accepted  -> retried with backoff, then GmailTransientError
* connection lost after DATA started          -> DeliveryUnknownError (never retried)
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Awaitable, Callable

from src.applications.gmail_client import (
    DeliveryUnknownError,
    GmailAuthError,
    GmailError,
    GmailPolicyError,
    GmailQuotaError,
    GmailRequestError,
    GmailSendingLimitError,
    GmailTransientError,
)
from src.utils.logger import get_logger, log_event, register_secrets
from src.utils.retry import backoff_delay

log = get_logger(__name__)

SMTP_HOST = "smtp.gmail.com"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024  # Gmail's message size limit for sending


def _text(exc: smtplib.SMTPResponseException) -> str:
    err = exc.smtp_error
    return err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)


def classify_smtp(code: int, text: str) -> GmailError:
    msg = f"Gmail SMTP {code}: {re.sub(r'\s+', ' ', text)[:300]}"
    low = text.lower()
    if code in (530, 534, 535):
        return GmailAuthError(msg + " - App Password rejected; create a new one and update GMAIL_APP_PASSWORD",
                              status=code)
    if "5.4.5" in text or "sending limit" in low or "quota" in low:
        return GmailSendingLimitError(msg, status=code, reason="sending_limit")
    if 500 <= code < 600 and "5.7." in text:
        return GmailPolicyError(msg, status=code, reason="policy")
    if 400 <= code < 500 and "4.7." in text:
        return GmailQuotaError(msg, status=code, reason="rate_limited")
    if code in (550, 553) and ("5.1." in text or "recipient" in low or "address" in low):
        return GmailRequestError(msg + " (invalid recipient)", status=code, reason="invalid_recipient")
    if 400 <= code < 500:
        return GmailTransientError(msg, status=code, reason="temporary")
    return GmailRequestError(msg, status=code)


class SmtpClient:
    def __init__(self, sender: str, app_password: str, *, host: str = SMTP_HOST, port: int = 587,
                 timeout: float = 60.0, max_attempts: int = 3, backoff_base: float = 2.0,
                 smtp_factory: Callable[..., Any] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not sender or "@" not in sender:
            raise GmailAuthError("GMAIL_SENDER_ADDRESS is not set in .env")
        password = re.sub(r"\s+", "", app_password or "")  # Google shows it as 4 groups of 4
        if len(password) < 16:
            raise GmailAuthError("GMAIL_APP_PASSWORD is missing or incomplete in .env (16 characters)")
        register_secrets([password, app_password])
        self.sender = sender
        self._password = password
        self.host, self.port, self.timeout = host, port, timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self._factory = smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)
        self._sleep = sleep

    def __repr__(self) -> str:  # never expose the password
        return f"SmtpClient(sender={self.sender!r}, host={self.host!r}, port={self.port})"

    def _send_once(self, msg: EmailMessage, stage: list[str]) -> None:
        ctx = ssl.create_default_context()
        stage[0] = "connect"
        if self.port == 465:
            server = self._factory(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            server = self._factory(self.host, self.port, timeout=self.timeout)
        with server as s:
            if self.port != 465:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
            stage[0] = "login"
            s.login(self.sender, self._password)
            stage[0] = "send"
            refused = s.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)

    async def send(self, msg: EmailMessage) -> None:
        if len(msg.as_bytes()) > MAX_MESSAGE_BYTES:
            raise GmailRequestError("Message is larger than Gmail's 25 MB limit")
        last: GmailError | None = None
        for attempt in range(1, self.max_attempts + 1):
            stage = ["init"]
            try:
                await asyncio.to_thread(self._send_once, msg, stage)
                log_event(log, 20, "smtp.sent", attempt=attempt)
                return
            except smtplib.SMTPRecipientsRefused as exc:
                classified: list[GmailError] = []
                for code, raw_msg in exc.recipients.values():
                    text_msg = raw_msg.decode("utf-8", "replace") if isinstance(raw_msg, bytes) else str(raw_msg)
                    classified.append(classify_smtp(code, text_msg))
                # Escalate account-level quota/policy/auth errors before falling back to invalid_recipient.
                for cls in (GmailAuthError, GmailSendingLimitError, GmailPolicyError, GmailQuotaError):
                    hit = next((e for e in classified if isinstance(e, cls)), None)
                    if hit is not None:
                        log_event(log, 30, "smtp.error", kind=hit.kind, status=hit.status)
                        raise hit from exc
                codes = [c for c, _ in exc.recipients.values()]
                raise GmailRequestError(f"Gmail SMTP refused the recipient (codes {codes}) (invalid recipient)",
                                        status=codes[0] if codes else None, reason="invalid_recipient") from exc
            except smtplib.SMTPResponseException as exc:
                err = classify_smtp(exc.smtp_code, _text(exc))
            except (smtplib.SMTPServerDisconnected, OSError) as exc:
                if stage[0] == "send":
                    raise DeliveryUnknownError(
                        f"Connection to Gmail SMTP was lost while sending ({type(exc).__name__}); the message may "
                        "or may not have been sent. Check the Gmail 'Sent' folder before re-queuing.") from exc
                err = GmailTransientError(f"Cannot reach Gmail SMTP during {stage[0]}: {type(exc).__name__}")
            except smtplib.SMTPException as exc:
                err = GmailTransientError(f"Gmail SMTP error during {stage[0]}: {type(exc).__name__}")
            if not isinstance(err, GmailTransientError):
                log_event(log, 30, "smtp.error", kind=err.kind, status=err.status)
                raise err
            last = err
            if attempt < self.max_attempts:
                delay = backoff_delay(attempt, self.backoff_base, cap=64.0)
                log_event(log, 30, "smtp.retry", attempt=attempt, delay=round(delay, 2))
                await self._sleep(delay)
        assert last is not None
        raise last
