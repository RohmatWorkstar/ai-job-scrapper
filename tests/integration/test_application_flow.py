"""End-to-end application flows with a mock mail transport, a fake clock and a fake AI provider.

No real e-mail is sent and no network is used: Gmail is replaced by mock transports or the
dry-run outbox, the AI provider by a deterministic fake.
"""

from __future__ import annotations

import asyncio
import email
from pathlib import Path
from typing import Any

import pytest

from src.applications.email_service import DryRunTransport, EmailService, SendRefused, TransportResult
from src.applications.gmail_client import DeliveryUnknownError, GmailSendingLimitError
from src.applications.manager import ApplicationManager
from src.applications.models import ApplicationStatus, file_sha256
from src.applications.scheduler import SendingScheduler
from src.applications.sending_limits import SendingGuard, SendingLimits
from src.applications.tracker import ApplicationTracker, DuplicateApplicationError
from src.core.models import JobOpportunity, SearchConfig
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.storage.database import Database
from src.storage.repository import Repository
from tests.unit.test_applications import Clock, fake_ai, make_pdf, new_record

S = ApplicationStatus


class MockTransport:
    name = "mock"

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.sent: list[Any] = []
        self.errors = errors or []

    async def send(self, msg) -> TransportResult:  # noqa: ANN001
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(msg)
        return TransportResult(message_id=f"mock-{len(self.sent)}", transport=self.name)


def limits(**kw: Any) -> SendingLimits:
    base = dict(max_per_day=3, max_per_hour=3, min_interval_seconds=60, max_per_recipient_domain_per_day=5,
                window_start_hour=None, window_end_hour=None)
    base.update(kw)
    return SendingLimits(**base)


@pytest.fixture
def env(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    tracker = ApplicationTracker(db)
    clock = Clock()
    transport = MockTransport()
    guard = SendingGuard(tracker, limits(), transport_name="mock", stop_file=tmp_path / "STOP", now=clock)
    service = EmailService(tracker, guard, transport, sender="me@gmail.com", sender_name="Andi Pratama")
    pdf = make_pdf(tmp_path / "cv.pdf")
    yield dict(db=db, tracker=tracker, clock=clock, transport=transport, guard=guard, service=service, pdf=pdf,
               tmp=tmp_path)
    db.close()


def approved(tracker: ApplicationTracker, pdf: Path, n: int = 0, **kw: Any):
    rec = new_record(tracker, pdf, company=f"Company {n}", recipient=f"jobs@c{n}.co.id", **kw)
    tracker.transition(rec.id, S.PENDING_APPROVAL)
    return tracker.approve(rec.id)


# ------------------------------------------------------------- send gate
def test_unapproved_applications_cannot_be_sent(env) -> None:
    rec = new_record(env["tracker"], env["pdf"])
    env["tracker"].transition(rec.id, S.PENDING_APPROVAL)
    with pytest.raises(SendRefused) as e:
        asyncio.run(env["service"].send_application(rec.id))
    assert e.value.code == "not_approved" and env["transport"].sent == []


def test_approved_send_records_history(env) -> None:
    rec = approved(env["tracker"], env["pdf"])
    sent = asyncio.run(env["service"].send_application(rec.id))
    assert sent.status == "SENT" and sent.gmail_message_id == "mock-1" and sent.message_id_header
    msg = env["transport"].sent[0]
    assert msg["To"] == "jobs@c0.co.id" and "Andi Pratama" in msg["From"]
    att = [p for p in msg.iter_attachments()]
    assert att[0].get_filename() == "Andi_Pratama_CV.pdf" and att[0].get_content().startswith(b"%PDF")
    with pytest.raises(SendRefused):  # SENT is terminal: never sent twice
        asyncio.run(env["service"].send_application(rec.id))


def test_changes_after_approval_are_refused(env) -> None:
    tr = env["tracker"]
    rec = approved(tr, env["pdf"])
    tr.conn.execute("UPDATE applications SET email_body='tampered' WHERE id=?", (rec.id,))
    with pytest.raises(SendRefused) as e:
        asyncio.run(env["service"].send_application(rec.id))
    assert e.value.code == "approval_mismatch"
    rec2 = approved(tr, env["pdf"], n=1)
    make_pdf(env["pdf"], "a different CV")  # the attached file changed on disk
    with pytest.raises(SendRefused) as e:
        asyncio.run(env["service"].send_application(rec2.id))
    assert e.value.code == "approval_mismatch" and env["transport"].sent == []


def test_quota_error_pauses_and_is_not_retried(env) -> None:
    env["transport"].errors = [GmailSendingLimitError("429 Mail sending", status=429)]
    rec = approved(env["tracker"], env["pdf"])
    rec2 = approved(env["tracker"], env["pdf"], n=1)
    with pytest.raises(GmailSendingLimitError):
        asyncio.run(env["service"].send_application(rec.id))
    failed = env["tracker"].get(rec.id)
    assert failed.status == "FAILED" and failed.error_kind == "sending_limit" and failed.send_lock is None
    env["clock"].advance(120)
    with pytest.raises(SendRefused) as e:
        asyncio.run(env["service"].send_application(rec2.id))
    assert e.value.code == "paused"
    with pytest.raises(SendRefused):  # a FAILED record is never re-sent without re-approval
        asyncio.run(env["service"].send_application(rec.id))


def test_unknown_delivery_keeps_lock_and_blocks_duplicates(env) -> None:
    env["transport"].errors = [DeliveryUnknownError("timeout after upload")]
    rec = approved(env["tracker"], env["pdf"])
    with pytest.raises(DeliveryUnknownError):
        asyncio.run(env["service"].send_application(rec.id))
    failed = env["tracker"].get(rec.id)
    assert failed.error_kind == "delivery_unknown" and failed.send_lock
    with pytest.raises(DuplicateApplicationError):  # same company+title cannot be queued again
        new_record(env["tracker"], env["pdf"], company="Company 0", recipient="other@c0.co.id")


def test_emergency_stop_blocks_sending(env) -> None:
    rec = approved(env["tracker"], env["pdf"])
    env["guard"].emergency_stop()
    with pytest.raises(SendRefused) as e:
        asyncio.run(env["service"].send_application(rec.id))
    assert e.value.code == "emergency_stop" and env["tracker"].get(rec.id).status == "APPROVED"


def test_dry_run_transport_writes_eml(env) -> None:
    tr = env["tracker"]
    outbox = env["tmp"] / "outbox"
    guard = SendingGuard(tr, limits(), transport_name="dry_run", now=env["clock"])
    service = EmailService(tr, guard, DryRunTransport(outbox), sender="dry-run@localhost.invalid")
    rec = approved(tr, env["pdf"])
    after = asyncio.run(service.send_application(rec.id))
    files = list(outbox.glob("*.eml"))
    assert after.transport == "dry_run" and len(files) == 1
    parsed = email.message_from_bytes(files[0].read_bytes())
    assert parsed["To"] == "jobs@c0.co.id" and parsed.is_multipart()
    # A dry run is a rehearsal: never SENT, approval withdrawn, still sendable for real later.
    assert after.status == "PENDING_APPROVAL" and after.approved_hash is None and after.send_lock is None
    assert any("Dry run done" in n for n in after.notes)
    new_record_blocked = pytest.raises(DuplicateApplicationError)
    with new_record_blocked:  # still the active application for this job: no second record
        new_record(tr, env["pdf"], company="Company 0", recipient="jobs@c0.co.id")
    live = EmailService(tr, env["guard"], env["transport"], sender="me@gmail.com")
    tr.approve(rec.id)
    assert asyncio.run(live.send_application(rec.id)).status == "SENT"


# ------------------------------------------------------ review-fix regressions
def test_requeue_after_unknown_delivery_can_be_sent_again(env) -> None:
    tr = env["tracker"]
    env["transport"].errors = [DeliveryUnknownError("timeout after upload")]
    rec = approved(tr, env["pdf"])
    with pytest.raises(DeliveryUnknownError):
        asyncio.run(env["service"].send_application(rec.id))
    back = tr.requeue(rec.id)  # user checked Gmail Sent: not delivered
    assert back.status == "PENDING_APPROVAL" and back.send_lock is None
    tr.approve(rec.id)
    env["clock"].advance(600)
    assert asyncio.run(env["service"].send_application(rec.id)).status == "SENT"


def test_attachment_changed_after_approval_returns_to_review(env) -> None:
    tr = env["tracker"]
    rec = approved(tr, env["pdf"])
    make_pdf(env["pdf"], "updated CV")
    report = asyncio.run(SendingScheduler(env["service"], tr).process_queue())
    assert report.sent == [] and tr.get(rec.id).status == "PENDING_APPROVAL"  # not stuck as APPROVED
    again = tr.approve(rec.id)  # approval binds to the file as it is now
    assert again.attachment_sha256 == file_sha256(str(env["pdf"]))
    assert asyncio.run(env["service"].send_application(rec.id)).status == "SENT"


def test_gmail_auth_failure_keeps_application_approved(env) -> None:
    from src.applications.gmail_client import GmailAuthError

    env["transport"].errors = [GmailAuthError("refresh failed")]
    rec = approved(env["tracker"], env["pdf"])
    with pytest.raises(GmailAuthError):
        asyncio.run(env["service"].send_application(rec.id))
    after = env["tracker"].get(rec.id)
    assert after.status == "APPROVED" and after.send_lock is None  # nothing was sent: no re-approval needed
    assert env["guard"].check().code == "paused"
    assert env["tracker"].last_attempts(5, transport="mock") == []  # not counted as a send attempt


def test_edit_refused_while_send_in_progress(env) -> None:
    from src.applications.tracker import InvalidTransitionError

    rec = approved(env["tracker"], env["pdf"])
    assert env["tracker"].claim_send_lock(rec.id)
    with pytest.raises(InvalidTransitionError):
        env["tracker"].set_content(rec.id, body="changed during send")


def test_freemail_recipients_do_not_collide_as_duplicates(env) -> None:
    from src.applications.tracker import make_dedupe_key

    assert make_dedupe_key(None, "Backend Developer", "a@gmail.com") != make_dedupe_key(None, "Backend Developer",
                                                                                         "b@gmail.com")
    tr = env["tracker"]
    new_record(tr, env["pdf"], company=None, recipient="a@gmail.com")
    new_record(tr, env["pdf"], company=None, recipient="b@gmail.com")  # different employer: allowed
    assert make_dedupe_key(None, "Dev", "x@acme.co.id") == make_dedupe_key(None, "Dev", "y@acme.co.id")


def test_interrupted_ai_preparation_does_not_block_the_job(settings, sample_cv_path, monkeypatch) -> None:
    db = Database(settings.database_path)
    try:
        cv, profile = candidate(settings, sample_cv_path)
        mgr = ApplicationManager(settings, db)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))

        async def interrupted(job):  # noqa: ANN001
            raise KeyboardInterrupt

        monkeypatch.setattr(mgr.analyzer, "analyze", interrupted)
        job = JobOpportunity(job_id="k1", job_title="Backend Developer", company_name="Acme",
                             application_emails=["jobs@acme.co.id"])
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(mgr.prepare_ai(job))
        rec = mgr.tracker.list()[0]
        assert rec.status == "FAILED" and rec.error_kind == "interrupted"
        assert mgr.tracker.find_blocking(rec.dedupe_key) is None
    finally:
        db.close()


# -------------------------------------------------------------- scheduler
def test_queue_respects_interval_and_daily_limit(env) -> None:
    clock = env["clock"]
    for n in range(5):
        approved(env["tracker"], env["pdf"], n=n)
    waits: list[float] = []

    async def fake_sleep(sec: float) -> None:
        waits.append(sec)
        clock.advance(sec)

    sched = SendingScheduler(env["service"], env["tracker"], sleep=fake_sleep)
    report = asyncio.run(sched.process_queue())
    assert len(report.sent) == 3 and report.remaining == 2
    assert "Daily limit" in report.stopped_reason
    assert len(waits) == 2 and all(w >= 60 for w in waits)  # minimum interval enforced between sends
    clock.advance(24 * 3600)
    report2 = asyncio.run(sched.process_queue())
    assert len(report2.sent) == 2 and report2.remaining == 0


def test_queue_stops_on_quota_error_and_emergency_stop(env) -> None:
    for n in range(3):
        approved(env["tracker"], env["pdf"], n=n)
    env["transport"].errors = [GmailSendingLimitError("429 (Mail sending)", status=429)]
    sched = SendingScheduler(env["service"], env["tracker"], sleep=lambda s: asyncio.sleep(0))
    report = asyncio.run(sched.process_queue())
    assert report.sent == [] and len(report.failed) == 1 and report.remaining == 2
    assert env["transport"].sent == []
    env["guard"].resume()
    env["guard"].emergency_stop()
    report = asyncio.run(sched.process_queue())
    assert report.sent == [] and "Emergency stop" in report.stopped_reason


# ---------------------------------------------------------- full flows
def seed_jobs(settings, db: Database) -> str:
    repo = Repository(db)
    sid = "2026-11-23_100000-abc123"
    repo.create_session(sid, None, SearchConfig())
    jobs = [
        JobOpportunity(job_id="good", job_title="Backend Developer", company_name="Acme Pay",
                       relevance_score=78, relevance_category="HIGHLY RELEVANT", verification_status="LIKELY_OPEN",
                       application_emails=["careers@acmepay.co.id"], required_skills=["Node.js", "PostgreSQL", "Docker"],
                       job_description="Requirements:\n- Node.js and PostgreSQL\n- Docker\nSend your CV to careers@acmepay.co.id"),
        JobOpportunity(job_id="noemail", job_title="Fullstack Developer", company_name="NoMail Inc",
                       relevance_score=80, relevance_category="HIGHLY RELEVANT", verification_status="VERIFIED_OPEN"),
        JobOpportunity(job_id="lowscore", job_title="Accountant", company_name="Numbers Ltd", relevance_score=20,
                       relevance_category="NOT RELEVANT", verification_status="LIKELY_OPEN",
                       application_emails=["hr@numbers.co.id"]),
        JobOpportunity(job_id="badmail", job_title="Node.js Developer", company_name="Platform Co", relevance_score=70,
                       relevance_category="RELEVANT", verification_status="LIKELY_OPEN",
                       application_emails=["no-reply@platform.co"]),
        JobOpportunity(job_id="scam", job_title="Backend Developer", company_name="Easy Money", relevance_score=75,
                       relevance_category="HIGHLY RELEVANT", verification_status="POSSIBLE_SCAM",
                       application_emails=["boss@easymoney.biz"]),
    ]
    repo.save_opportunities(sid, jobs)
    repo.finish_session(sid, "completed", {}, None)
    return sid


def candidate(settings, sample_cv_path):
    cv = parse_cv(sample_cv_path)
    return cv, CVAnalyzer(settings).analyze(cv)


def test_basic_mode_end_to_end(settings, sample_cv_path, tmp_path: Path) -> None:
    db = Database(settings.database_path)
    try:
        sid = seed_jobs(settings, db)
        cv, profile = candidate(settings, sample_cv_path)
        mgr = ApplicationManager(settings, db)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))
        pdf = make_pdf(tmp_path / "cv" / "Andi_CV.pdf")
        original_sha = file_sha256(str(pdf))
        session, jobs = mgr.load_jobs()
        assert session == sid and len(jobs) == 5
        summary = mgr.prepare_basic(jobs, pdf, session_id=sid)
        assert [r.job_key for r in summary.prepared] == ["good"]
        assert summary.no_email == 1 and len(summary.invalid_email) == 1 and len(summary.rejected) == 2
        rec = summary.prepared[0]
        assert rec.status == "PENDING_APPROVAL" and rec.recipient_email == "careers@acmepay.co.id"
        assert rec.attachment_path == str(pdf) and rec.mode == "basic"
        assert "Backend Developer" in rec.email_subject and "Acme Pay" in rec.email_body
        counts = mgr.tracker.counts()
        assert counts == {"PENDING_APPROVAL": 1, "SKIPPED": 1, "REJECTED": 2}
        # Second run: nothing new (duplicate prevention, no repeated rejected rows).
        again = mgr.prepare_basic(jobs, pdf, session_id=sid)
        assert again.prepared == [] and len(again.duplicates) == 1
        assert mgr.tracker.counts() == counts
        # Approve and send through the dry-run outbox.
        mgr.tracker.approve(rec.id)
        guard = SendingGuard(mgr.tracker, limits(), transport_name="dry_run", now=Clock())
        service = EmailService(mgr.tracker, guard, DryRunTransport(tmp_path / "outbox"), sender="me@gmail.com")
        ok, msg = asyncio.run(SendingScheduler(service, mgr.tracker).send_one(rec.id))
        assert ok and "DRY RUN" in msg and mgr.tracker.get(rec.id).status == "PENDING_APPROVAL"
        assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1
        assert file_sha256(str(pdf)) == original_sha  # original CV untouched
    finally:
        db.close()


AI_ANSWERS = {
    "Analyse this job posting": {"required_skills": ["Node.js", "PostgreSQL", "Docker"],
                                 "requirements": ["Node.js and PostgreSQL"], "keywords": ["Docker"]},
    "Tailor the candidate's CV": {"summary": "", "skills_order": ["Node.js", "PostgreSQL", "Docker"],
                                  "bullets": [{"id": "e0b1", "text": "Designed REST APIs on PostgreSQL with Node.js, Express and Prisma."}]},
    "job application e-mail": {"subject": "Application for Backend Developer", "body":
                               "Dear Hiring Team,\n\nI am applying for the Backend Developer position at Acme Pay. As a "
                               "Fullstack Developer, I designed REST APIs with Node.js, Express and Prisma on PostgreSQL "
                               "and introduced Docker-based local environments with GitHub Actions CI/CD. My CV is "
                               "attached, and I would be glad to discuss the role."},
}


def test_ai_mode_end_to_end(settings, sample_cv_path, tmp_path: Path) -> None:
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "generated")
    db = Database(settings.database_path)
    try:
        seed_jobs(settings, db)
        cv, profile = candidate(settings, sample_cv_path)
        ai, prov = fake_ai(AI_ANSWERS)
        mgr = ApplicationManager(settings, db, ai=ai)
        asyncio.run(mgr.set_candidate(cv, profile, sample_cv_path, use_ai=False))
        _, jobs = mgr.load_jobs()
        good = next(j for j in jobs if j.job_id == "good")
        rec = asyncio.run(mgr.prepare_ai(good))
        assert rec is not None and rec.status == "PENDING_APPROVAL", rec.error
        assert rec.generated_cv_path and Path(rec.generated_cv_path).name == "Acme_Pay_Backend_Developer_Custom_CV.pdf"
        assert rec.attachment_path == rec.generated_cv_path and rec.attachment_sha256 == file_sha256(rec.generated_cv_path)
        assert rec.match_score and rec.match["match"]["components"] and rec.match["email_method"] == "ai"
        assert "Kind regards,\nAndi Pratama" in rec.email_body
        statuses = [e["to_status"] for e in mgr.tracker.events(rec.id)]
        assert statuses == ["NEW", "ANALYZED", "MATCHED", "PENDING_APPROVAL"]
        assert all("andi.pratama@example.com" not in p for p in prov.prompts)  # contact details never sent to AI
        # Regenerating the CV withdraws an approval.
        mgr.tracker.approve(rec.id)
        regen = asyncio.run(mgr.regenerate_cv(rec.id))
        assert regen.status == "PENDING_APPROVAL" and regen.approved_hash is None
        # A low-match job is REJECTED before any CV is generated.
        mgr.min_match = 101
        low = asyncio.run(mgr.prepare_ai(JobOpportunity(job_id="x2", job_title="Data Scientist", company_name="Other",
                                                        application_emails=["jobs@other.co.id"])))
        assert low is not None and low.status == "REJECTED" and low.generated_cv_path is None
    finally:
        db.close()


# ------------------------------------------------------------------- CLI
def _no_side_effects(monkeypatch) -> None:
    """Review tests must never open a PDF viewer or an editor (that would block the test run)."""
    import src.cli.apply_cli as cli

    def forbidden(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("must not open files/editors in tests")

    monkeypatch.setattr(cli, "open_file", forbidden)
    monkeypatch.setattr(cli.subprocess, "run", forbidden)


def _drive_review(keys: str, recs, mgr, sched) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from src.cli.apply_cli import review

    with create_pipe_input() as inp:
        inp.send_text(keys)
        with create_app_session(input=inp, output=DummyOutput()):
            asyncio.run(review(recs, mgr, sched, dry_run=True, interactive=True))


DOWN = "\x1b[B"
# Menu for a Basic application with an attachment, manual mode:
# 0 Approve and send now, 1 Approve (queue), 2 Edit e-mail, 3 Open CV PDF, 4 Decide later,
# 5 Reject, 6 Skip, 7 Stop reviewing


def test_interactive_review_approve_send_and_reject(env, settings, monkeypatch) -> None:
    """Drive the real review prompts: 1st Enter (approve + send), 2nd Decide later, 3rd Reject."""
    _no_side_effects(monkeypatch)
    tr = env["tracker"]
    recs = []
    for n in range(3):
        r = new_record(tr, env["pdf"], company=f"Company {n}", recipient=f"jobs@c{n}.co.id")
        recs.append(tr.transition(r.id, S.PENDING_APPROVAL))
    mgr = ApplicationManager(settings, env["db"])
    sched = SendingScheduler(env["service"], tr, sleep=lambda s: asyncio.sleep(0))
    _drive_review("\r" + DOWN * 4 + "\r" + DOWN * 5 + "\r", recs, mgr, sched)
    first, second, third = (tr.get(r.id) for r in recs)
    assert first.status == "SENT" and first.approved_by == "user (CLI review)"
    assert second.status == "PENDING_APPROVAL"                      # decide later keeps it
    assert third.status == "REJECTED" and third.approval_status == "rejected"
    assert len(env["transport"].sent) == 1


def test_review_without_sending_setup_still_approves_to_queue(env, settings, monkeypatch) -> None:
    _no_side_effects(monkeypatch)
    tr = env["tracker"]
    r = tr.transition(new_record(tr, env["pdf"]).id, S.PENDING_APPROVAL)
    # scheduler=None: menu starts with "Approve (queue)"; Enter approves without sending.
    _drive_review("\r", [r], ApplicationManager(settings, env["db"]), None)
    assert tr.get(r.id).status == "APPROVED" and env["transport"].sent == []


def test_prompts_work_from_inside_the_event_loop(settings, tmp_path: Path) -> None:
    """Regression: questionary .ask() fails inside a running loop; main_flow/apply run prompts via to_thread."""
    import argparse

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from src.cli.application import Application, parse_args

    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    for name in ("A_CV.txt", "B_CV.txt"):
        (cv_dir / name).write_text("Andi Pratama\nBackend Developer\n" + "Built REST APIs with Node.js. " * 10,
                                   encoding="utf-8")
    settings.raw["candidate"]["cv_directory"] = str(cv_dir)
    app = Application(parse_args([]))
    app.settings, app.interactive, app.saved = settings, True, {}

    async def main() -> Any:
        return await asyncio.to_thread(app.choose_cv)

    with create_pipe_input() as inp:
        inp.send_text("\x1b[B\r")  # Down, Enter -> second CV
        with create_app_session(input=inp, output=DummyOutput()):
            cv = asyncio.run(main())
    assert isinstance(app.args, argparse.Namespace) and cv is not None and cv.filename == "B_CV.txt"


def test_cli_basic_dry_run_non_interactive(settings, sample_cv_path, tmp_path: Path, monkeypatch) -> None:
    import pymupdf

    from src.cli import application as app_mod

    cv_dir = tmp_path / "cvdir"
    cv_dir.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), sample_cv_path.read_text(encoding="utf-8"), fontsize=9)
    doc.save(str(cv_dir / "Andi_CV.pdf"))
    doc.close()
    settings.raw["candidate"]["cv_directory"] = str(cv_dir)
    settings.raw["applications"]["sending"]["outbox_directory"] = str(tmp_path / "outbox")
    settings.raw["applications"]["sending"]["emergency_stop_file"] = str(tmp_path / "STOP")
    settings.raw["applications"]["sending"]["sending_window"] = {"start_hour": 0, "end_hour": 24,
                                                                  "weekdays": [0, 1, 2, 3, 4, 5, 6]}
    db = Database(settings.database_path)
    seed_jobs(settings, db)
    db.close()
    monkeypatch.setattr(app_mod, "load_settings", lambda: settings)

    assert app_mod.run(["--apply", "basic", "--defaults", "--dry-run", "--cv", "Andi_CV.pdf"]) == 0
    db = Database(settings.database_path)
    tracker = ApplicationTracker(db)
    pending = tracker.list([S.PENDING_APPROVAL])
    assert len(pending) == 1 and tracker.list([S.SENT]) == []  # nothing is approved or sent automatically
    tracker.approve(pending[0].id)
    db.close()
    assert app_mod.run(["--emergency-stop", "--defaults"]) == 0
    assert app_mod.run(["--send-queue", "--defaults", "--dry-run"]) == 0
    assert list((tmp_path / "outbox").glob("*.eml")) == []  # blocked by the emergency stop
    assert app_mod.run(["--resume-sending", "--defaults"]) == 0
    assert app_mod.run(["--send-queue", "--defaults", "--dry-run"]) == 0
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1
    db = Database(settings.database_path)
    assert ApplicationTracker(db).list([S.SENT]) == []  # a dry run never marks anything SENT
    db.close()
    assert app_mod.run(["--applications", "--defaults"]) == 0
    assert app_mod.run(["--sending-status", "--defaults"]) == 0
    assert app_mod.run(["--setup-check", "--defaults"]) == 0  # CV + jobs with e-mail present


def test_cli_live_mode_requires_gmail_setup(settings, monkeypatch, tmp_path: Path) -> None:
    from src.cli import application as app_mod

    settings.raw["applications"]["sending"]["dry_run"] = False
    settings.raw["applications"]["gmail"] = {"client_secret_file": str(tmp_path / "c.json"),
                                             "token_file": str(tmp_path / "t.json")}
    monkeypatch.setattr(app_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GMAIL_SENDER_ADDRESS", "me@gmail.com")
    monkeypatch.delenv("GMAIL_TOKEN_FILE", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET_FILE", raising=False)
    assert app_mod.run(["--send-queue", "--defaults"]) == 1  # not authorised -> clear error, nothing sent
