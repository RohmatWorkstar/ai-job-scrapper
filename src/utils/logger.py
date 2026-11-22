"""Structured (JSON lines) logging with secret and PII redaction.

Logs go to ``data/logs/app_YYYYMMDD.log`` only; the terminal UI is rendered by
Rich separately so log noise never reaches the user.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
_KEY_PARAM_RE = re.compile(r"(?i)((?:api[_-]?key|key|token|access_token|secret)=)[^&\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")

_SECRETS: list[str] = []


def register_secrets(values: Iterable[str]) -> None:
    """Register secret values that must never appear in logs."""
    for v in values:
        if v and v not in _SECRETS:
            _SECRETS.append(v)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    text = _KEY_PARAM_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _PHONE_RE.sub("<phone>", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = json.loads(redact(json.dumps(extra, default=str, ensure_ascii=False)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def setup_logging(log_dir: Path, level: int = logging.INFO, secrets: Iterable[str] = ()) -> Path:
    """Configure root logging to a daily JSON-lines file. Returns the log file path."""
    global _configured
    register_secrets(secrets)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"
    root = logging.getLogger()
    if not _configured:
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        root.setLevel(level)
        # Third-party HTTP libraries log full URLs; keep them quiet.
        for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "fitz", "pymupdf"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **data: object) -> None:
    logger.log(level, msg, extra={"data": data} if data else None)
