"""Sending safety controls: rolling daily/hourly caps, minimum interval, per-domain cap,
sending window, automatic pause and the emergency stop.

Every send path (manual, scheduled, automatic) asks :meth:`SendingGuard.check` first and
reports the outcome through :meth:`SendingGuard.record`. Counters are derived from the
``send_attempts`` table, so they survive restarts.

Documented Gmail limits (checked 2026-11-23, see FINAL_DEVELOPMENT_REPORT.md section 6):
* personal Gmail: errors may start above 500 recipients/emails per day (support.google.com/mail/answer/22839)
* Google Workspace: 2,000 messages/day per user, 500 on trial accounts, rolling 24 hours
These are ceilings at which Google *blocks* sending, not safe operating targets. The
configured caps below are refused when they exceed half of the ceiling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from src.applications.tracker import ApplicationTracker
from src.core.configuration import Settings
from src.core.exceptions import ConfigurationError
from src.utils.date_time import from_iso, iso, to_local, utc_now
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)

DOCUMENTED_DAILY_CEILING = {"personal": 500, "workspace": 2000, "workspace_trial": 500}
# Hard upper bound accepted in configuration: half of the documented ceiling.
MAX_ALLOWED_FRACTION = 0.5
# Above this daily volume the CLI shows a deliverability / account-risk warning.
HIGH_VOLUME_WARNING = 50


def recipient_key(recipient: str) -> str:
    """Per-employer key for the per-domain cap: "@acme.co.id", or the full address for free-mail
    domains (many unrelated recruiters use @gmail.com; they must not share one cap)."""
    from src.applications.contacts import FREEMAIL_DOMAINS

    addr = recipient.strip().lower()
    domain = addr.rsplit("@", 1)[1]
    plain = re.sub(r"\+[^@]*@", "@", addr)  # name+tag@gmail.com is the same mailbox as name@gmail.com
    return plain if domain in FREEMAIL_DOMAINS else "@" + domain


@dataclass
class SendingLimits:
    account_type: str = "personal"
    max_per_day: int = 20
    max_per_hour: int = 6
    min_interval_seconds: int = 180
    max_per_recipient_domain_per_day: int = 2
    max_consecutive_failures: int = 3
    max_invalid_recipients_per_day: int = 3
    window_start_hour: int | None = 8
    window_end_hour: int | None = 18
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    quota_pause_hours: float = 24.0

    @property
    def documented_ceiling(self) -> int:
        return DOCUMENTED_DAILY_CEILING[self.account_type]

    def warnings(self) -> list[str]:
        out = []
        if self.max_per_day > HIGH_VOLUME_WARNING:
            out.append(f"max_per_day={self.max_per_day}: unsolicited e-mail at this volume raises the risk of spam "
                       "reports, filtering and account restrictions. Start lower and increase gradually.")
        if self.min_interval_seconds < 60:
            out.append("min_interval_seconds below 60 makes sending look automated and bursty.")
        return out

    @classmethod
    def from_settings(cls, settings: Settings) -> "SendingLimits":
        s = settings.get("applications.sending", {}) or {}
        window = s.get("sending_window") or {}
        acct = str(s.get("account_type", "personal")).lower()
        if acct not in DOCUMENTED_DAILY_CEILING:
            raise ConfigurationError(f"applications.sending.account_type must be one of {sorted(DOCUMENTED_DAILY_CEILING)}")
        lim = cls(
            account_type=acct,
            max_per_day=int(s.get("max_per_day", 20)),
            max_per_hour=int(s.get("max_per_hour", 6)),
            min_interval_seconds=int(s.get("min_interval_seconds", 180)),
            max_per_recipient_domain_per_day=int(s.get("max_per_recipient_domain_per_day", 2)),
            max_consecutive_failures=int(s.get("max_consecutive_failures", 3)),
            max_invalid_recipients_per_day=int(s.get("max_invalid_recipients_per_day", 3)),
            window_start_hour=window.get("start_hour", 8),
            window_end_hour=window.get("end_hour", 18),
            weekdays=list(window.get("weekdays", [0, 1, 2, 3, 4])),
            quota_pause_hours=float(s.get("quota_pause_hours", 24)),
        )
        lim.validate()
        return lim

    def validate(self) -> None:
        ceiling = int(self.documented_ceiling * MAX_ALLOWED_FRACTION)
        if not 1 <= self.max_per_day <= ceiling:
            raise ConfigurationError(
                f"applications.sending.max_per_day must be between 1 and {ceiling} for a {self.account_type} account "
                f"(half of Google's documented limit of {self.documented_ceiling}/day; that limit blocks sending, "
                "it is not a safe target).")
        if not 1 <= self.max_per_hour <= self.max_per_day:
            raise ConfigurationError("applications.sending.max_per_hour must be between 1 and max_per_day")
        if self.min_interval_seconds < 10:
            raise ConfigurationError("applications.sending.min_interval_seconds must be at least 10")
        if self.max_per_recipient_domain_per_day < 1 or self.max_consecutive_failures < 1:
            raise ConfigurationError("applications.sending per-domain and failure limits must be at least 1")
        start, end = self.window_start_hour, self.window_end_hour
        if (start is None) != (end is None):
            raise ConfigurationError("applications.sending.sending_window needs both start_hour and end_hour (or neither)")
        if start is not None and end is not None:
            if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
                raise ConfigurationError("applications.sending.sending_window hours must be whole numbers (e.g. 8, 18)")
            if not 0 <= start < end <= 24:
                raise ConfigurationError("applications.sending.sending_window must satisfy 0 <= start_hour < end_hour <= 24")
            if not self.weekdays or not all(isinstance(d, int) and 0 <= d <= 6 for d in self.weekdays):
                raise ConfigurationError("applications.sending.sending_window.weekdays must list days 0-6 (Monday=0)")


@dataclass
class Decision:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    wait_seconds: float = 0.0

    @property
    def retry_at_hint(self) -> str:
        return f"in {int(self.wait_seconds // 60)} min {int(self.wait_seconds % 60)} s" if self.wait_seconds else ""


class SendingGuard:
    EMERGENCY_KEY = "emergency_stop"
    PAUSE_KEY = "paused"

    def __init__(self, tracker: ApplicationTracker, limits: SendingLimits, *, transport_name: str,
                 stop_file: Path | None = None, timezone: str = "Asia/Jakarta",
                 now: Callable[[], datetime] = utc_now) -> None:
        self.tracker = tracker
        self.limits = limits
        self.transport_name = transport_name
        self.stop_file = stop_file
        self.timezone = timezone
        self._now = now

    @classmethod
    def from_settings(cls, settings: Settings, tracker: ApplicationTracker, transport_name: str,
                      now: Callable[[], datetime] = utc_now) -> "SendingGuard":
        stop = settings.path("applications.sending.emergency_stop_file", "data/STOP_SENDING")
        return cls(tracker, SendingLimits.from_settings(settings), transport_name=transport_name, stop_file=stop,
                   timezone=settings.timezone, now=now)

    # ------------------------------------------------------------- controls
    def emergency_stop(self, reason: str = "manual emergency stop") -> None:
        self.tracker.set_control(self.EMERGENCY_KEY, json.dumps({"reason": reason, "at": iso(self._now())}))
        log_event(log, 30, "sending.emergency_stop", reason=reason)

    def clear_emergency_stop(self) -> None:
        self.tracker.set_control(self.EMERGENCY_KEY, None)
        if self.stop_file and self.stop_file.exists():
            self.stop_file.unlink()

    def pause(self, reason: str, hours: float | None = None) -> None:
        until = iso(self._now() + timedelta(hours=hours)) if hours else None
        self.tracker.set_control(self.PAUSE_KEY, json.dumps({"reason": reason, "at": iso(self._now()), "until": until}))
        log_event(log, 30, "sending.paused", reason=reason, until=until)

    def resume(self) -> None:
        self.tracker.set_control(self.PAUSE_KEY, None)

    def _control(self, key: str) -> dict[str, Any] | None:
        raw = self.tracker.get_control(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return {"reason": raw}

    def pause_state(self) -> dict[str, Any] | None:
        p = self._control(self.PAUSE_KEY)
        if p and p.get("until"):
            until = from_iso(p["until"])
            if until and until <= self._now():
                return None
        return p

    # --------------------------------------------------------------- checks
    def _attempts(self, hours: float, outcomes: tuple[str, ...]) -> list[dict[str, Any]]:
        since = iso(self._now() - timedelta(hours=hours)) or ""
        return self.tracker.attempts_since(since, outcomes=outcomes, transport=self.transport_name)

    @property
    def _counted(self) -> tuple[str, ...]:
        # A blocked or failed attempt may still have reached Gmail, so it counts against the caps too.
        return ("sent", "failed", "blocked")

    def check(self, recipient: str | None = None) -> Decision:
        now = self._now()
        if self.stop_file is not None and self.stop_file.exists():
            return Decision(False, "emergency_stop", f"Emergency stop file present: {self.stop_file}")
        es = self._control(self.EMERGENCY_KEY)
        if es:
            return Decision(False, "emergency_stop", f"Emergency stop active ({es.get('reason', '')}). "
                                                     "Clear it with: python main.py --resume-sending")
        p = self.pause_state()
        if p:
            wait = 0.0
            if p.get("until"):
                until = from_iso(p["until"])
                wait = max(0.0, (until - now).total_seconds()) if until else 0.0
            return Decision(False, "paused", f"Sending paused: {p.get('reason', '')}"
                            + (f" (until {p['until']})" if p.get("until") else "; resume with --resume-sending"), wait)
        lim = self.limits
        local = to_local(now, self.timezone)
        if local is not None and lim.window_start_hour is not None and lim.window_end_hour is not None:
            in_window = lim.window_start_hour <= local.hour < lim.window_end_hour and local.weekday() in lim.weekdays
            if not in_window:
                return Decision(False, "outside_window",
                                f"Outside the sending window ({lim.window_start_hour:02d}:00-{lim.window_end_hour:02d}:00 "
                                f"local, weekdays {lim.weekdays}).")
        day = self._attempts(24, self._counted)
        if len(day) >= lim.max_per_day:
            oldest = from_iso(day[0]["attempted_at"])
            wait = ((oldest + timedelta(hours=24)) - now).total_seconds() if oldest else 3600
            return Decision(False, "daily_limit", f"Daily limit reached ({lim.max_per_day} in the last 24 hours).",
                            max(wait, 0))
        hour = [a for a in day if (from_iso(a["attempted_at"]) or now) >= now - timedelta(hours=1)]
        if len(hour) >= lim.max_per_hour:
            oldest = from_iso(hour[0]["attempted_at"])
            wait = ((oldest + timedelta(hours=1)) - now).total_seconds() if oldest else 600
            return Decision(False, "hourly_limit", f"Hourly limit reached ({lim.max_per_hour} in the last hour).",
                            max(wait, 0))
        last = self.tracker.last_attempts(1, transport=self.transport_name)
        if last:
            prev = from_iso(last[0]["attempted_at"])
            if prev:
                elapsed = (now - prev).total_seconds()
                if elapsed < lim.min_interval_seconds:
                    return Decision(False, "min_interval",
                                    f"Minimum interval of {lim.min_interval_seconds} s between e-mails.",
                                    lim.min_interval_seconds - elapsed)
        if recipient and "@" in recipient:
            domain = recipient_key(recipient)
            same = [a for a in day if (a.get("recipient_domain") or "") == domain and a["outcome"] == "sent"]
            if len(same) >= lim.max_per_recipient_domain_per_day:
                return Decision(False, "domain_limit",
                                f"Already sent {len(same)} e-mail(s) to {domain} in 24 hours "
                                f"(max_per_recipient_domain_per_day={lim.max_per_recipient_domain_per_day}).")
        return Decision(True)

    # -------------------------------------------------------------- results
    def record(self, app_id: str | None, outcome: str, recipient: str | None, *, error_kind: str | None = None,
               error: str | None = None) -> None:
        """Store an attempt and apply automatic pauses for suspicious patterns."""
        domain = recipient_key(recipient) if recipient and "@" in recipient else None
        self.tracker.record_attempt(app_id, outcome, self.transport_name, domain, error_kind, error, iso(self._now()))
        lim = self.limits
        recent = self.tracker.last_attempts(lim.max_consecutive_failures, transport=self.transport_name)
        if len(recent) >= lim.max_consecutive_failures and all(a["outcome"] != "sent" for a in recent):
            self.pause(f"{lim.max_consecutive_failures} consecutive failed sends; check the errors before resuming")
            return
        invalid = [a for a in self._attempts(24, ("failed",)) if a.get("error_kind") == "invalid_recipient"]
        if len(invalid) >= lim.max_invalid_recipients_per_day:
            self.pause(f"{len(invalid)} invalid-recipient failures in 24 hours (bounces hurt sender reputation)")

    def status(self) -> dict[str, Any]:
        lim = self.limits
        day = self._attempts(24, self._counted)
        hour = self._attempts(1, self._counted)
        return {
            "transport": self.transport_name,
            "account_type": lim.account_type,
            "sent_or_attempted_24h": len(day),
            "max_per_day": lim.max_per_day,
            "sent_or_attempted_1h": len(hour),
            "max_per_hour": lim.max_per_hour,
            "min_interval_seconds": lim.min_interval_seconds,
            "paused": self.pause_state(),
            "emergency_stop": self._control(self.EMERGENCY_KEY)
            or ({"reason": f"stop file {self.stop_file}"} if self.stop_file and self.stop_file.exists() else None),
            "next_decision": self.check().__dict__,
        }
