"""Tests for Pillar 4 (schema v3 migration, send_lock guard, Web/ATS assist, lifecycle, cross-channel blocking)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.applications.email_service import (
    DryRunTransport,
    EmailService,
    SendRefused,
    TestRedirectTransport as RedirectTransport,
    TransportResult,
)
from src.applications.manager import ApplicationManager
from src.applications.models import ApplicationMode, ApplicationStatus, file_sha256
from src.applications.sending_limits import SendingGuard, SendingLimits
from src.applications.setup_check import setup_checklist, validate_applications_settings
from src.applications.tracker import (
    ApplicationTracker,
    DuplicateApplicationError,
    InvalidTransitionError,
    make_dedupe_key,
)
from src.core.exceptions import ConfigurationError
from src.core.models import JobOpportunity, SearchConfig, SourceType
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.storage.database import Database
from src.storage.repository import Repository
from tests.unit.test_applications import Clock, make_pdf, new_record

S = ApplicationStatus


class MockTransport:
    name = "mock"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, msg) -> TransportResult:  # noqa: ANN001
        self.sent.append(msg)
        return TransportResult(message_id=f"mock-{len(self.sent)}", transport=self.name)


def test_schema_v2_upgrades_in_place_to_v3_without_data_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v2.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta(key, value) VALUES ('schema_version', '2');
        CREATE TABLE applications (
            id TEXT PRIMARY KEY, job_key TEXT, session_id TEXT, company_name TEXT, job_title TEXT,
            job_url TEXT, recipient_email TEXT, mode TEXT NOT NULL, match_score REAL, match_json TEXT,
            original_cv_path TEXT, original_cv_sha256 TEXT, generated_cv_path TEXT, attachment_path TEXT,
            attachment_sha256 TEXT, email_subject TEXT, email_body TEXT, status TEXT NOT NULL,
            approval_status TEXT NOT NULL DEFAULT 'none', approved_at TEXT, approved_by TEXT, approved_hash TEXT,
            dedupe_key TEXT NOT NULL, allow_duplicate INTEGER NOT NULL DEFAULT 0, duplicate_authorization TEXT,
            send_lock TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, error_kind TEXT,
            gmail_message_id TEXT, message_id_header TEXT, transport TEXT, notes_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT
        );
        INSERT INTO applications(id, job_key, company_name, job_title, recipient_email, mode, status, dedupe_key,
                                 created_at, updated_at)
        VALUES ('old1', 'jk1', 'Acme', 'Backend Engineer', 'hr@acme.id', 'basic', 'SENT', 'acme|backend engineer',
                '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    try:
        ver = db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        assert ver == "3"
        rec = ApplicationTracker(db).get("old1")
        assert rec is not None
        assert rec.channel == "email"
        assert rec.closed_by is None
        assert rec.sender_name is None
        assert rec.follow_up_at is None
    finally:
        db.close()


def test_tracker_send_lock_guard_plus_tag_dedupe_and_score_priority(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite3")
    try:
        tr = ApplicationTracker(db)
        pdf = make_pdf(tmp_path / "cv.pdf")
        r_low = new_record(tr, pdf, company="Alpha", recipient="hr@alpha.io", match_score=62.0)
        r_high = new_record(tr, pdf, company="Beta", recipient="hr@beta.io", match_score=91.0)
        for r in (r_low, r_high):
            tr.transition(r.id, S.PENDING_APPROVAL)
            tr.approve(r.id)
        # Queue orders by match_score DESC
        queued = tr.list([S.APPROVED])
        assert [r.id for r in queued] == [r_high.id, r_low.id]

        # Claim lock -> concurrent skip/transition without allow_locked MUST fail
        assert tr.claim_send_lock(r_high.id)
        with pytest.raises(InvalidTransitionError):
            tr.skip(r_high.id, "concurrent skip")

        # update_fields forbids allow_duplicate
        with pytest.raises(ValueError, match="allow_duplicate"):
            tr.update_fields(r_low.id, allow_duplicate=1)

        # Gmail +tag normalization blocks duplicate recipient
        new_record(tr, pdf, company="Gamma", recipient="recruiter@gmail.com")
        with pytest.raises(DuplicateApplicationError):
            new_record(tr, pdf, company="Delta", recipient="recruiter+jobs@gmail.com")

        # URL fallback prevents "?|<title>" collisions for company-less web jobs
        k1 = make_dedupe_key(None, "Backend Engineer", None, "https://boards.a.test/job/1")
        k2 = make_dedupe_key(None, "Backend Engineer", None, "https://boards.b.test/job/2")
        assert k1 != k2 and not k1.startswith("?|")
    finally:
        db.close()


def test_web_assist_lifecycle_queue_guard_and_cross_channel_blocking(settings, sample_cv_path, tmp_path: Path) -> None:
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")
    db = Database(tmp_path / "web.sqlite3")
    try:
        cv = parse_cv(sample_cv_path)
        profile = CVAnalyzer(settings).analyze(cv)
        mgr = ApplicationManager(settings, db)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))

        job_web = JobOpportunity(
            job_id="w1",
            job_title="Fullstack Developer",
            company_name="Xendit",
            source_type=SourceType.ATS.value,
            source_name="Greenhouse (xendit)",
            source_url="https://boards.greenhouse.io/xendit/jobs/1",
            application_url="https://boards.greenhouse.io/xendit/jobs/1",
            relevance_score=85.0,
            relevance_category="HIGHLY RELEVANT",
            verification_status="VERIFIED_OPEN",
            required_skills=["Node.js", "PostgreSQL", "React", "TypeScript"],
            job_description="Build payment services with Node.js, React, TypeScript, and PostgreSQL.",
        )
        rec = asyncio.run(mgr.prepare_web(job_web, mode=ApplicationMode.AI, session_id="s1"))
        assert rec is not None and rec.status == "PENDING_APPROVAL"
        assert rec.channel == "web" and rec.mode == "ai" and rec.recipient_email is None
        assert rec.generated_cv_path and Path(rec.generated_cv_path).exists()
        assert rec.sender_name == "Andi Pratama"
        assert "Xendit" in rec.email_body

        # Web applications are strictly refused by approve() and auto_approve()
        with pytest.raises(InvalidTransitionError):
            mgr.tracker.approve(rec.id)
        with pytest.raises(InvalidTransitionError):
            mgr.auto_approve([rec])

        # Mark as applied on web -> INTERVIEW -> OFFER -> WITHDRAWN
        applied = mgr.tracker.mark_applied_web(rec.id, note="Submitted via Greenhouse form")
        assert applied.status == "APPLIED_WEB" and applied.sent_at
        inter = mgr.tracker.update_pipeline_status(rec.id, S.INTERVIEW, note="Tech screen booked", follow_up_at="2026-12-01")
        assert inter.status == "INTERVIEW" and inter.follow_up_at == "2026-12-01"
        offer = mgr.tracker.update_pipeline_status(rec.id, S.OFFER, note="Offer received")
        assert offer.status == "OFFER"
        withdrawn = mgr.tracker.update_pipeline_status(rec.id, S.WITHDRAWN, note="Accepted another offer")
        assert withdrawn.status == "WITHDRAWN"

        # Cross-channel blocking: WITHDRAWN web application still blocks creating an email application for the same job
        job_web_with_mail = JobOpportunity.from_dict(job_web.to_dict())
        job_web_with_mail.application_emails = ["jobs@xendit.co"]
        with pytest.raises(DuplicateApplicationError):
            asyncio.run(mgr.prepare_ai(job_web_with_mail, session_id="s2"))
    finally:
        db.close()


def test_system_rejection_in_basic_does_not_block_ai_or_web_but_user_rejection_does(
    settings, sample_cv_path, tmp_path: Path
) -> None:
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")
    db = Database(tmp_path / "closed.sqlite3")
    try:
        cv = parse_cv(sample_cv_path)
        profile = CVAnalyzer(settings).analyze(cv)
        pdf = make_pdf(tmp_path / "cv.pdf")
        mgr = ApplicationManager(settings, db)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))

        job_low = JobOpportunity(
            job_id="low1",
            job_title="Fullstack Developer",
            company_name="Nusantara Tech",
            source_type=SourceType.ATS.value,
            source_name="ATS",
            source_url="https://nusantara.test/jobs/1",
            application_url="https://nusantara.test/jobs/1",
            relevance_score=45.0,  # below Basic min_relevance_score=58
            relevance_category="POTENTIALLY RELEVANT",
            verification_status="LIKELY_OPEN",
            application_emails=["jobs@nusantara.test"],
            required_skills=["Node.js", "PostgreSQL", "React", "TypeScript"],
            job_description="Build Node.js, React, TypeScript and PostgreSQL web apps.",
        )
        # Basic rejects automatically with closed_by='system'
        sum_basic = mgr.prepare_basic([job_low], pdf, session_id="s1")
        assert len(sum_basic.rejected) == 1
        sys_rec = mgr.tracker.list()[0]
        assert sys_rec.closed_by == "system"

        # AI mode evaluates the same job cleanly (not permanently locked out!)
        ai_rec = asyncio.run(mgr.prepare_ai(job_low, session_id="s1"))
        assert ai_rec is not None and ai_rec.status == "PENDING_APPROVAL"

        # If the USER explicitly rejects it, cross-channel web mode IS blocked
        mgr.tracker.reject(ai_rec.id, "not interested", closed_by="user")
        web_rec = asyncio.run(mgr.prepare_web(job_low, mode=ApplicationMode.AI, session_id="s2"))
        assert web_rec is None
    finally:
        db.close()


def test_channel_all_shared_cap_prioritizes_email_and_falls_back_to_web(settings, sample_cv_path, tmp_path: Path) -> None:
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")
    db = Database(tmp_path / "all.sqlite3")
    try:
        cv = parse_cv(sample_cv_path)
        profile = CVAnalyzer(settings).analyze(cv)
        mgr = ApplicationManager(settings, db)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))

        j_email = JobOpportunity(
            job_id="e1", job_title="Fullstack Developer", company_name="MailCorp", relevance_score=90.0,
            relevance_category="HIGHLY RELEVANT", verification_status="LIKELY_OPEN",
            source_url="https://mailcorp.test/1", application_url="https://mailcorp.test/1",
            application_emails=["jobs@mailcorp.test"], required_skills=["Node.js", "React", "PostgreSQL"],
            job_description="Node.js React PostgreSQL Fullstack Developer.",
        )
        j_web1 = JobOpportunity(
            job_id="w1", job_title="Backend Developer", company_name="WebCorp", relevance_score=85.0,
            relevance_category="HIGHLY RELEVANT", verification_status="VERIFIED_OPEN",
            source_url="https://webcorp.test/1", application_url="https://webcorp.test/1",
            required_skills=["Node.js", "PostgreSQL", "Docker"],
            job_description="Node.js PostgreSQL Docker Backend Developer.",
        )
        j_web2 = JobOpportunity(
            job_id="w2", job_title="Backend Developer", company_name="ThirdCorp", relevance_score=70.0,
            relevance_category="RELEVANT", verification_status="VERIFIED_OPEN",
            source_url="https://thirdcorp.test/1", application_url="https://thirdcorp.test/1",
            required_skills=["Node.js", "PostgreSQL"],
            job_description="Node.js PostgreSQL Backend Developer.",
        )
        summary = asyncio.run(
            mgr.prepare_batch([j_web2, j_web1, j_email], mode=ApplicationMode.AI, channel="all", limit=2, session_id="s1")
        )
        assert len(summary.prepared) == 2
        assert [(r.job_key, r.channel) for r in summary.prepared] == [("e1", "email"), ("w1", "web")]
    finally:
        db.close()


def test_test_redirect_does_not_pollute_employer_domain_cap_and_preserves_sender_name_crlf(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "send.sqlite3")
    try:
        tr = ApplicationTracker(db)
        pdf = make_pdf(tmp_path / "cv.pdf")
        clock = Clock()
        lim = SendingLimits(
            max_per_day=5, max_per_hour=5, min_interval_seconds=10,
            max_per_recipient_domain_per_day=1, window_start_hour=None, window_end_hour=None,
        )
        guard = SendingGuard(tr, lim, transport_name="mock", now=clock)
        inner = MockTransport()
        # Allow self-test recipient (same as sender) in setup_check
        monkeypatch.setenv("GMAIL_SENDER_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("APPLICATION_TEST_RECIPIENT", "me@gmail.com")
        items = {i.name: i for i in setup_checklist(settings)}
        assert items["TEST MODE (recipient redirect)"].ok

        redirect = RedirectTransport(inner, "me@gmail.com")
        # Service has empty sender_name, record carries sender_name="Budi Santoso"
        service = EmailService(
            tr, guard, redirect, sender="me@gmail.com", sender_name="", reply_to="budi@custom.id"
        )
        r1 = new_record(tr, pdf, company="Acme", recipient="hr@acme.id", sender_name="Budi Santoso")
        tr.transition(r1.id, S.PENDING_APPROVAL)
        tr.approve(r1.id)
        asyncio.run(service.send_application(r1.id))
        assert "Budi Santoso" in inner.sent[0]["From"]
        assert inner.sent[0]["Reply-To"] == "budi@custom.id"
        assert [p for p in inner.sent[0].iter_attachments()][0].get_filename() == "Budi_Santoso_CV.pdf"

        # Second test send for same employer domain is NOT blocked by domain cap (max_per_recipient_domain_per_day=1)
        clock.advance(20)
        tr.approve(r1.id)
        asyncio.run(service.send_application(r1.id))
        assert len(inner.sent) == 2

        # Real send after test sends is STILL allowed for acme.id (domain counter wasn't polluted!)
        clock.advance(20)
        assert guard.check("hr@acme.id").allowed

        # DryRunTransport outputs strict CRLF (\r\n)
        outbox = tmp_path / "outbox"
        dry_guard = SendingGuard(tr, lim, transport_name="dry_run", now=clock)
        dry_svc = EmailService(tr, dry_guard, DryRunTransport(outbox), sender="me@gmail.com")
        tr.approve(r1.id)
        asyncio.run(dry_svc.send_application(r1.id))
        raw_eml = next(outbox.glob("*.eml")).read_bytes()
        assert b"\r\n" in raw_eml

        # Preflight refuses unresolved placeholders
        bad = tr.create(
            mode="basic", company_name="PlaceholderCo", job_title="Backend Developer", recipient_email="jobs@ph.id",
            email_subject="Application for Backend Developer",
            email_body="Dear [Name], please consider my application for this role.",
            attachment_path=str(pdf), attachment_sha256=file_sha256(str(pdf)),
        )
        tr.transition(bad.id, S.PENDING_APPROVAL)
        tr.approve(bad.id)
        with pytest.raises(SendRefused) as exc:
            dry_svc.preflight(tr.get(bad.id))
        assert exc.value.code == "invalid_message"
    finally:
        db.close()


def test_settings_validation_checks_cooldown_and_18mb_cap(settings) -> None:
    settings.raw["applications"]["recipient_cooldown_days"] = -5
    settings.raw["applications"]["sending"]["max_attachment_mb"] = 22
    with pytest.raises(ConfigurationError) as exc:
        validate_applications_settings(settings)
    msg = str(exc.value)
    assert "recipient_cooldown_days" in msg and "max_attachment_mb" in msg


def test_tracker_status_precedence_and_global_snapshot(tmp_path: Path) -> None:
    db = Database(tmp_path / "snap.sqlite3")
    try:
        tr = ApplicationTracker(db)
        pdf = make_pdf(tmp_path / "cv.pdf")
        r_sys = new_record(tr, pdf, job_key="jk1", company="Acme", recipient="a@acme.id")
        tr.reject(r_sys.id, "auto rejected", closed_by="system")

        r_web = tr.create(
            mode="ai", channel="web", job_key="jk1", company_name="Acme", job_title="Backend Engineer",
            job_url="https://acme.id/jobs/1", status="PENDING_APPROVAL",
            allow_duplicate=True, duplicate_authorization="test",
        )
        tr.mark_applied_web(r_web.id)
        tr.update_pipeline_status(r_web.id, S.INTERVIEW, note="HR call")

        statuses = tr.latest_status_by_job()
        assert statuses["jk1"] == "INTERVIEW (web)"

        snap = tr.global_snapshot()
        assert len(snap) == 1  # system auto-rejected row excluded!
        assert snap[0]["id"] == r_web.id and snap[0]["status"] == "INTERVIEW"
    finally:
        db.close()
