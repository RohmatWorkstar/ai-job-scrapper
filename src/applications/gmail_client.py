"""Gmail API client: OAuth 2.0 (installed-app flow) and ``users.messages.send``.

* Scope: only ``https://www.googleapis.com/auth/gmail.send`` (least privilege; a *sensitive* scope).
* Credentials: the OAuth client JSON and the token file live under ``data/secrets/`` (git-ignored)
  or wherever GMAIL_CLIENT_SECRET_FILE / GMAIL_TOKEN_FILE point. Nothing is hard-coded.
* Sending uses the documented media-upload endpoint
  ``POST https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=media``
  with ``Content-Type: message/rfc822`` (documented maximum 35 MB).

Error handling follows https://developers.google.com/workspace/gmail/api/guides/handle-errors :
* 429 "... (Mail sending)"           -> GmailSendingLimitError: sending is paused, never retried.
* 403 dailyLimitExceeded/domainPolicy -> GmailQuotaError / GmailPolicyError: paused, never retried.
* 403 rate limit, 429 other, 5xx      -> bounded exponential backoff (retried), then GmailTransientError.
* 401                                 -> one token refresh, then GmailAuthError.
* 400                                 -> GmailRequestError (e.g. invalid recipient), not retried.
* connection never established        -> retried; timeout/drop after the upload -> DeliveryUnknownError
                                         (not retried, because the message may already have been sent).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from src.core.exceptions import JobIntelError
from src.utils.logger import get_logger, log_event
from src.utils.retry import backoff_delay

log = get_logger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [GMAIL_SEND_SCOPE]
SEND_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
MAX_MESSAGE_BYTES = 35 * 1024 * 1024


# ------------------------------------------------------------------ errors
class GmailError(JobIntelError):
    kind = "gmail_error"
    pause_sending = False

    def __init__(self, message: str, *, status: int | None = None, reason: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class GmailAuthError(GmailError):
    kind = "auth"
    pause_sending = True


class GmailSendingLimitError(GmailError):
    """The account hit Gmail's mail-sending limit (shared with web/SMTP). Never retried."""

    kind = "sending_limit"
    pause_sending = True


class GmailQuotaError(GmailError):
    kind = "quota"
    pause_sending = True


class GmailPolicyError(GmailError):
    kind = "policy"
    pause_sending = True


class GmailRequestError(GmailError):
    kind = "invalid_request"


class GmailTransientError(GmailError):
    kind = "transient"


class DeliveryUnknownError(GmailError):
    """The request may have reached Gmail; the message might have been sent. Not retried."""

    kind = "delivery_unknown"


# -------------------------------------------------------------------- auth
class GmailAuth:
    """Loads, refreshes and stores OAuth credentials using Google's official libraries."""

    def __init__(self, client_secret_file: Path, token_file: Path) -> None:
        self.client_secret_file = client_secret_file
        self.token_file = token_file
        self._creds: Any = None

    @classmethod
    def from_settings(cls, settings: Any) -> "GmailAuth":
        secret = os.environ.get("GMAIL_CLIENT_SECRET_FILE", "").strip() or str(
            settings.path("applications.gmail.client_secret_file", "data/secrets/gmail_client_secret.json"))
        token = os.environ.get("GMAIL_TOKEN_FILE", "").strip() or str(
            settings.path("applications.gmail.token_file", "data/secrets/gmail_token.json"))
        root = settings.root
        sp, tp = Path(secret), Path(token)
        return cls(sp if sp.is_absolute() else root / sp, tp if tp.is_absolute() else root / tp)

    @staticmethod
    def _libs() -> tuple[Any, Any, Any]:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise GmailAuthError("Gmail support needs: pip install google-auth google-auth-oauthlib") from exc
        return Request, Credentials, InstalledAppFlow

    def _save(self, creds: Any) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - Windows ignores POSIX modes
            pass
        tmp.replace(self.token_file)

    def authorize_interactive(self, open_browser: bool = True) -> None:
        """Run the OAuth consent flow in the browser (loopback redirect on localhost)."""
        Request, Credentials, InstalledAppFlow = self._libs()
        if not self.client_secret_file.exists():
            raise GmailAuthError(
                f"OAuth client file not found: {self.client_secret_file}. Create a 'Desktop app' OAuth client in "
                "Google Cloud Console, download its JSON and save it there (see FINAL_DEVELOPMENT_REPORT.md section 10).")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secret_file), scopes=SCOPES)
        creds = flow.run_local_server(host="localhost", port=0, open_browser=open_browser,
                                      authorization_prompt_message="Opening the Google consent page: {url}",
                                      success_message="Gmail authorisation complete. You can close this tab.")
        self._save(creds)
        self._creds = creds
        log_event(log, 20, "gmail.authorized")

    def has_token(self) -> bool:
        return self.token_file.exists()

    def access_token(self, force_refresh: bool = False) -> str:
        Request, Credentials, _ = self._libs()
        if self._creds is None:
            if not self.token_file.exists():
                raise GmailAuthError("Gmail is not authorised yet. Run: python main.py --gmail-auth")
            try:
                self._creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            except (ValueError, OSError) as exc:
                raise GmailAuthError(f"Gmail token file is unreadable ({exc}); run --gmail-auth again") from exc
        creds = self._creds
        if GMAIL_SEND_SCOPE not in (creds.scopes or SCOPES):
            raise GmailAuthError("Stored Gmail token lacks the gmail.send scope; run --gmail-auth again")
        if force_refresh or not creds.valid:
            if not creds.refresh_token:
                raise GmailAuthError("Gmail token expired and has no refresh token; run --gmail-auth again")
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001 - google.auth.exceptions.RefreshError and network errors
                raise GmailAuthError(
                    "Gmail token refresh failed (revoked, or a 7-day 'Testing' token expired). "
                    f"Run: python main.py --gmail-auth  [{type(exc).__name__}]") from exc
            self._save(creds)
        return str(creds.token)


# ------------------------------------------------------------------ client
def _error_info(resp: httpx.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error", {})
    except (ValueError, json.JSONDecodeError):
        return "", resp.text[:200]
    reasons = [e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)]
    status = str(err.get("status") or "")
    return (reasons[0] if reasons else status), str(err.get("message", ""))[:300]


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class GmailClient:
    def __init__(self, token_provider: Callable[[bool], str], *, transport: httpx.AsyncBaseTransport | None = None,
                 max_attempts: int = 3, backoff_base: float = 1.0, timeout: float = 60.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._token = token_provider
        self._transport = transport
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.timeout = timeout
        self._sleep = sleep

    def classify(self, resp: httpx.Response) -> GmailError:
        reason, message = _error_info(resp)
        status = resp.status_code
        ra = _retry_after(resp)
        text = f"Gmail API HTTP {status} {reason}: {message}".strip()
        if status == 429 and "mail sending" in message.lower():
            return GmailSendingLimitError(text, status=status, reason=reason, retry_after=ra)
        if status == 403 and reason == "dailyLimitExceeded":
            return GmailQuotaError(text, status=status, reason=reason, retry_after=ra)
        if status == 403 and reason == "domainPolicy":
            return GmailPolicyError(text, status=status, reason=reason)
        if status in (403, 429) and reason in ("rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED", ""):
            return GmailTransientError(text, status=status, reason=reason or "rate_limited", retry_after=ra)
        if status == 429:
            return GmailTransientError(text, status=status, reason=reason, retry_after=ra)
        if status == 401:
            return GmailAuthError(text, status=status, reason=reason)
        if status == 403:
            return GmailPolicyError(text, status=status, reason=reason)
        if status == 400:
            return GmailRequestError(text, status=status, reason=reason)
        if status >= 500:
            return GmailTransientError(text, status=status, reason=reason or "backendError")
        return GmailRequestError(text, status=status, reason=reason)

    async def send_raw(self, mime_bytes: bytes) -> str:
        """Send an RFC 822 message. Returns the Gmail message id."""
        if len(mime_bytes) > MAX_MESSAGE_BYTES:
            raise GmailRequestError(f"Message is {len(mime_bytes)} bytes; Gmail's upload limit is 35 MB")
        refreshed = False
        last: GmailError | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), transport=self._transport) as client:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    token = self._token(False)
                except GmailError:
                    raise
                try:
                    resp = await client.post(SEND_URL, params={"uploadType": "media"}, content=mime_bytes,
                                             headers={"Authorization": f"Bearer {token}",
                                                      "Content-Type": "message/rfc822"})
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    last = GmailTransientError(f"Cannot connect to Gmail: {type(exc).__name__}")
                except httpx.HTTPError as exc:
                    # Upload may have completed: never resend automatically.
                    raise DeliveryUnknownError(
                        f"Connection to Gmail failed after the upload started ({type(exc).__name__}); the message may "
                        "or may not have been sent. Check the Gmail 'Sent' folder before re-queuing.") from exc
                else:
                    if 200 <= resp.status_code < 300:
                        try:
                            msg_id = str(resp.json().get("id", ""))
                        except ValueError:
                            msg_id = ""
                        log_event(log, 20, "gmail.sent", attempt=attempt)
                        return msg_id
                    err = self.classify(resp)
                    if isinstance(err, GmailAuthError) and not refreshed:
                        refreshed = True
                        self._token(True)
                        continue
                    if not isinstance(err, GmailTransientError):
                        log_event(log, 30, "gmail.error", kind=err.kind, status=err.status, reason=err.reason)
                        raise err
                    last = err
                if attempt < self.max_attempts:
                    delay = backoff_delay(attempt, self.backoff_base, cap=64.0,
                                          retry_after=last.retry_after if last else None)
                    log_event(log, 30, "gmail.retry", attempt=attempt, delay=round(delay, 2),
                              reason=last.reason if last else "")
                    await self._sleep(delay)
        if last is None:
            raise GmailAuthError("Gmail rejected the credentials even after a token refresh; run --gmail-auth again")
        if last.status in (403, 429):
            raise GmailQuotaError(f"Gmail rate limit persisted after {self.max_attempts} attempts: {last}",
                                  status=last.status, reason=last.reason, retry_after=last.retry_after)
        raise last
