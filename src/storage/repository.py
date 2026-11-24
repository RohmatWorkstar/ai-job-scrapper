"""Data access for profiles, sessions, queries, discoveries, opportunities and reports."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from src.core.exceptions import StorageError
from src.core.models import ExecutedQuery, JobOpportunity, RawDiscovery, SearchConfig, SourceRunReport
from src.cv.models import CandidateProfile
from src.storage.database import Database
from src.utils.date_time import iso, utc_now


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.conn = db.conn

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        try:
            with self.conn:
                return self.conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise StorageError(f"Database error: {exc}") from exc

    def _many(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        try:
            with self.conn:
                self.conn.executemany(sql, rows)
        except sqlite3.Error as exc:
            raise StorageError(f"Database error: {exc}") from exc

    # ---------------------------------------------------------------- profiles
    def save_profile(self, profile: CandidateProfile) -> int:
        cur = self._exec(
            "INSERT INTO candidate_profiles(cv_filename, cv_sha256, analysis_method, profile_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (profile.cv_filename, profile.cv_sha256, profile.analysis_method, _dumps(profile.to_dict()), iso(utc_now())),
        )
        return int(cur.lastrowid)

    def latest_profile_for(self, sha256: str) -> CandidateProfile | None:
        row = self.conn.execute(
            "SELECT profile_json FROM candidate_profiles WHERE cv_sha256=? ORDER BY id DESC LIMIT 1", (sha256,)
        ).fetchone()
        return CandidateProfile.from_dict(json.loads(row["profile_json"])) if row else None

    # ---------------------------------------------------------------- sessions
    def create_session(self, session_id: str, profile_id: int | None, config: SearchConfig) -> None:
        self._exec("INSERT INTO search_sessions(id, profile_id, started_at, status, config_json) VALUES (?,?,?,?,?)",
                   (session_id, profile_id, iso(utc_now()), "running", _dumps(config.to_dict())))

    def finish_session(self, session_id: str, status: str, stats: dict[str, Any], report_dir: str | None) -> None:
        self._exec("UPDATE search_sessions SET completed_at=?, status=?, stats_json=?, report_dir=? WHERE id=?",
                   (iso(utc_now()), status, _dumps(stats), report_dir, session_id))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM search_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM search_sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def profile_by_id(self, profile_id: int) -> CandidateProfile | None:
        row = self.conn.execute("SELECT profile_json FROM candidate_profiles WHERE id=?", (profile_id,)).fetchone()
        return CandidateProfile.from_dict(json.loads(row["profile_json"])) if row else None

    # ----------------------------------------------------------------- queries
    def save_queries(self, session_id: str, queries: list[ExecutedQuery]) -> None:
        self._many(
            "INSERT INTO search_queries(session_id, source_name, category, query_text, status, result_count, error, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(session_id, q.source_name, q.category, q.query, q.status, q.result_count, q.error, q.executed_at) for q in queries],
        )

    def session_queries(self, session_id: str) -> list[ExecutedQuery]:
        rows = self.conn.execute("SELECT * FROM search_queries WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [ExecutedQuery(r["source_name"], r["category"], r["query_text"], r["status"], r["result_count"],
                              r["executed_at"], r["error"]) for r in rows]

    # ------------------------------------------------------------ discoveries
    def save_raw(self, session_id: str, items: list[RawDiscovery]) -> None:
        now = iso(utc_now())
        self._many("INSERT INTO raw_discoveries(session_id, source_name, source_url, data_json, created_at) VALUES (?,?,?,?,?)",
                   [(session_id, d.source_name, d.source_url, _dumps(d.to_dict()), now) for d in items])

    def raw_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT data_json FROM raw_discoveries WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ----------------------------------------------------------- opportunities
    def known_job_keys(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = "SELECT job_key, first_seen_at, times_seen, last_verification_status FROM opportunities WHERE job_key IN (%s)" % (
                ",".join("?" * len(chunk)))
            for r in self.conn.execute(q, chunk).fetchall():
                out[r["job_key"]] = dict(r)
        return out

    def save_opportunities(self, session_id: str, jobs: list[JobOpportunity]) -> None:
        now = iso(utc_now())
        opp_rows, sess_rows, src_rows, ver_rows = [], [], [], []
        for j in jobs:
            data = _dumps(j.to_dict())
            opp_rows.append((j.job_id, iso(j.first_seen_at) or now, now, session_id, session_id, j.verification_status,
                             j.job_title, j.company_name, j.source_url, data))
            sess_rows.append((session_id, j.job_id, int(j.is_new), j.relevance_category, j.relevance_score,
                              j.verification_status, data))
            src_rows.append((j.job_id, session_id, j.source_type, j.source_name, j.source_url, iso(j.discovered_at),
                             iso(j.published_at)))
            for s in j.additional_sources:
                src_rows.append((j.job_id, session_id, s.source_type, s.source_name, s.source_url, s.discovered_at,
                                 s.published_at))
            ver_rows.append((j.job_id, session_id, j.verification_status, j.verification_reason, now))
        self._many(
            "INSERT INTO opportunities(job_key, first_seen_at, last_seen_at, first_session_id, last_session_id, "
            "last_verification_status, job_title, company_name, source_url, data_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_key) DO UPDATE SET last_seen_at=excluded.last_seen_at, last_session_id=excluded.last_session_id, "
            "times_seen=opportunities.times_seen+1, last_verification_status=excluded.last_verification_status, "
            "job_title=excluded.job_title, company_name=excluded.company_name, data_json=excluded.data_json",
            opp_rows,
        )
        self._many("INSERT OR REPLACE INTO session_opportunities VALUES (?,?,?,?,?,?,?)", sess_rows)
        self._many("INSERT OR IGNORE INTO source_references VALUES (?,?,?,?,?,?,?)", src_rows)
        self._many("INSERT INTO verification_history(job_key, session_id, status, reason, checked_at) VALUES (?,?,?,?,?)",
                   ver_rows)

    def get_opportunity(self, job_key: str) -> JobOpportunity | None:
        row = self.conn.execute("SELECT data_json FROM opportunities WHERE job_key=?", (job_key,)).fetchone()
        return JobOpportunity.from_dict(json.loads(row["data_json"])) if row else None

    def session_opportunities(self, session_id: str) -> list[JobOpportunity]:
        rows = self.conn.execute("SELECT data_json FROM session_opportunities WHERE session_id=?", (session_id,)).fetchall()
        return [JobOpportunity.from_dict(json.loads(r["data_json"])) for r in rows]

    def previous_opportunities(self, exclude_session: str, since_iso: str, limit: int = 500) -> list[JobOpportunity]:
        rows = self.conn.execute(
            "SELECT data_json FROM opportunities WHERE last_session_id != ? AND last_seen_at >= ? "
            "AND last_verification_status NOT IN ('CLOSED','POSSIBLE_SCAM') ORDER BY last_seen_at DESC LIMIT ?",
            (exclude_session, since_iso, limit),
        ).fetchall()
        return [JobOpportunity.from_dict(json.loads(r["data_json"])) for r in rows]

    def recent_opportunities(self, days: int = 14, limit: int = 500) -> list[JobOpportunity]:
        """Deduplicated opportunities discovered within the last ``days`` days across all sessions."""
        from datetime import timedelta

        since = iso(utc_now() - timedelta(days=max(1, int(days)))) or ""
        rows = self.conn.execute(
            "SELECT data_json FROM opportunities WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        jobs = [JobOpportunity.from_dict(json.loads(r["data_json"])) for r in rows]
        jobs.sort(key=lambda j: -(j.relevance_score or 0))
        return jobs

    def verification_history(self, job_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM verification_history WHERE job_key=? ORDER BY id", (job_key,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ run metadata
    def save_source_runs(self, session_id: str, reports: list[SourceRunReport]) -> None:
        self._many("INSERT OR REPLACE INTO source_runs VALUES (?,?,?,?,?)",
                   [(session_id, r.name, r.category, r.status, _dumps(r.to_dict())) for r in reports])
        errors = [(session_id, r.name, e, iso(utc_now())) for r in reports for e in r.errors]
        self._many("INSERT INTO search_errors(session_id, source_name, message, occurred_at) VALUES (?,?,?,?)", errors)

    def source_runs(self, session_id: str) -> list[SourceRunReport]:
        rows = self.conn.execute("SELECT data_json FROM source_runs WHERE session_id=?", (session_id,)).fetchall()
        return [SourceRunReport(**json.loads(r["data_json"])) for r in rows]

    def save_report(self, session_id: str, kind: str, path: str) -> None:
        self._exec("INSERT INTO reports(session_id, kind, path, created_at) VALUES (?,?,?,?)",
                   (session_id, kind, path, iso(utc_now())))
