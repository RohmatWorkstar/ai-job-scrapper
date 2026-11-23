"""SQLite connection management and schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.core.exceptions import StorageError

SCHEMA_VERSION = 2

# Version 2 (application automation) adds new tables only; version-1 tables are untouched,
# so existing databases upgrade in place through CREATE TABLE IF NOT EXISTS.
APPLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_key TEXT,
    session_id TEXT,
    company_name TEXT,
    job_title TEXT,
    job_url TEXT,
    recipient_email TEXT,
    mode TEXT NOT NULL,
    match_score REAL,
    match_json TEXT,
    original_cv_path TEXT,
    original_cv_sha256 TEXT,
    generated_cv_path TEXT,
    attachment_path TEXT,
    attachment_sha256 TEXT,
    email_subject TEXT,
    email_body TEXT,
    status TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'none',
    approved_at TEXT,
    approved_by TEXT,
    approved_hash TEXT,
    dedupe_key TEXT NOT NULL,
    allow_duplicate INTEGER NOT NULL DEFAULT 0,
    duplicate_authorization TEXT,
    send_lock TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    error_kind TEXT,
    gmail_message_id TEXT,
    message_id_header TEXT,
    transport TEXT,
    notes_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_app_dedupe ON applications(dedupe_key);
CREATE INDEX IF NOT EXISTS ix_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS ix_app_job ON applications(job_key);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_app_events ON application_events(application_id);

CREATE TABLE IF NOT EXISTS send_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    transport TEXT NOT NULL,
    recipient_domain TEXT,
    error_kind TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_send_attempts_time ON send_attempts(attempted_at);

CREATE TABLE IF NOT EXISTS sending_control (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS candidate_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cv_filename TEXT NOT NULL,
    cv_sha256 TEXT NOT NULL,
    analysis_method TEXT,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_profiles_sha ON candidate_profiles(cv_sha256);

CREATE TABLE IF NOT EXISTS search_sessions (
    id TEXT PRIMARY KEY,
    profile_id INTEGER REFERENCES candidate_profiles(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    stats_json TEXT,
    report_dir TEXT
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES search_sessions(id),
    source_name TEXT NOT NULL,
    category TEXT NOT NULL,
    query_text TEXT NOT NULL,
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    executed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_queries_session ON search_queries(session_id);

CREATE TABLE IF NOT EXISTS raw_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES search_sessions(id),
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_session ON raw_discoveries(session_id);

CREATE TABLE IF NOT EXISTS opportunities (
    job_key TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_session_id TEXT NOT NULL,
    last_session_id TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1,
    last_verification_status TEXT,
    job_title TEXT,
    company_name TEXT,
    source_url TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_opportunities (
    session_id TEXT NOT NULL REFERENCES search_sessions(id),
    job_key TEXT NOT NULL REFERENCES opportunities(job_key),
    is_new INTEGER NOT NULL,
    relevance_category TEXT,
    relevance_score REAL,
    verification_status TEXT,
    data_json TEXT NOT NULL,
    PRIMARY KEY (session_id, job_key)
);

CREATE TABLE IF NOT EXISTS source_references (
    job_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_type TEXT,
    source_name TEXT,
    url TEXT NOT NULL,
    discovered_at TEXT,
    published_at TEXT,
    PRIMARY KEY (job_key, url)
);

CREATE TABLE IF NOT EXISTS verification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verif_job ON verification_history(job_key);

CREATE TABLE IF NOT EXISTS search_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_name TEXT,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_runs (
    session_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    category TEXT,
    status TEXT,
    data_json TEXT NOT NULL,
    PRIMARY KEY (session_id, source_name)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.migrate()
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open database {path}: {exc}") from exc

    def migrate(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.executescript(APPLICATION_SCHEMA)
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
