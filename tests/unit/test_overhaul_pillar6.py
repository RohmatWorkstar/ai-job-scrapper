"""Tests for Pillar 6 (Interactive Hub, CLI multi-channel flags, Web/ATS review, --no-ai / consent enforcement)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.applications.manager import ApplicationManager
from src.applications.models import ApplicationMode, ApplicationStatus
from src.applications.tracker import ApplicationTracker
from src.cli import application as app_mod
from src.cli import apply_cli
from src.cli.hub import InteractiveHub, filter_jobs, hub_snapshot
from src.core.exceptions import JobIntelError
from src.core.models import JobOpportunity, SearchConfig, SourceType
from src.core.orchestrator import Orchestrator
from src.cv.models import CandidateProfile
from src.storage.database import Database
from src.storage.repository import Repository

S = ApplicationStatus


def test_resolve_mode_channel_orthogonality() -> None:
    assert apply_cli.ApplyCommands.resolve_mode_channel("basic", None) == (ApplicationMode.BASIC, "email")
    assert apply_cli.ApplyCommands.resolve_mode_channel("ai", None) == (ApplicationMode.AI, "email")
    assert apply_cli.ApplyCommands.resolve_mode_channel("web", None) == (ApplicationMode.AI, "web")
    assert apply_cli.ApplyCommands.resolve_mode_channel("web", "web") == (ApplicationMode.AI, "web")
    assert apply_cli.ApplyCommands.resolve_mode_channel("ai", "all") == (ApplicationMode.AI, "all")
    with pytest.raises(JobIntelError, match="--apply web"):
        apply_cli.ApplyCommands.resolve_mode_channel("web", "email")


def test_ai_consent_non_interactive_defaults_to_false_unless_explicit(settings) -> None:
    settings.credentials = replace(settings.credentials, gemini_api_key="fake-key")
    app = app_mod.Application(app_mod.parse_args(["--defaults"]))
    app.settings = settings
    app.interactive = False
    app.saved = {}
    assert app.ai_consent() is False

    app_yes = app_mod.Application(app_mod.parse_args(["--defaults", "--ai-consent"]))
    app_yes.settings = settings
    app_yes.interactive = False
    app_yes.saved = {}
    assert app_yes.ai_consent() is True

    app_no_ai = app_mod.Application(app_mod.parse_args(["--defaults", "--ai-consent", "--no-ai"]))
    app_no_ai.settings = settings
    app_no_ai.interactive = False
    app_no_ai.saved = {"ai_cv_consent": True}
    assert app_no_ai.ai_consent() is False


def test_orchestrator_use_ai_false_never_calls_gemini(settings, tmp_path: Path) -> None:
    import httpx

    settings.credentials = replace(settings.credentials, gemini_api_key="fake-gemini-key")
    settings.raw["discovery"]["connectors"] = []
    settings.raw["reporting"]["output_directory"] = str(tmp_path / "reports")
    gemini_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal gemini_calls
        if "googleapis" in str(req.url):
            gemini_calls += 1
        return httpx.Response(200, json={})

    async def fake_resolver(_h: str) -> list[str]:
        return ["93.184.216.34"]

    orch = Orchestrator(settings, transport=httpx.MockTransport(handler), resolver=fake_resolver)
    try:
        res = asyncio.run(
            orch.run(CandidateProfile(current_title="Backend Engineer", skills=["Python"]), SearchConfig(), use_ai=False)
        )
        assert gemini_calls == 0
        assert res.data is not None and "disabled" in res.data.ai_status.lower()
    finally:
        orch.close()


def test_cli_apply_web_and_update_status_end_to_end(settings, sample_cv_path, tmp_path: Path, monkeypatch) -> None:
    import pymupdf

    cv_dir = tmp_path / "cvdir"
    cv_dir.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), sample_cv_path.read_text(encoding="utf-8"), fontsize=9)
    doc.save(str(cv_dir / "Andi_CV.pdf"))
    doc.close()
    settings.raw["candidate"]["cv_directory"] = str(cv_dir)
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")

    db = Database(settings.database_path)
    repo = Repository(db)
    repo.create_session("sess_web", None, SearchConfig())
    repo.save_opportunities(
        "sess_web",
        [
            JobOpportunity(
                job_id="w_only",
                job_title="Fullstack Developer",
                company_name="Flip",
                country="Indonesia",
                source_type=SourceType.ATS.value,
                source_name="Ashby (flip)",
                source_url="https://jobs.ashbyhq.com/flip/1",
                application_url="https://jobs.ashbyhq.com/flip/1",
                relevance_score=84.0,
                relevance_category="HIGHLY RELEVANT",
                verification_status="VERIFIED_OPEN",
                required_skills=["Node.js", "React", "TypeScript", "PostgreSQL"],
                job_description="Build fintech services with Node.js, React, TypeScript, and PostgreSQL.",
            )
        ],
    )
    repo.finish_session("sess_web", "completed", {"unique_opportunities": 1}, None)
    db.close()

    monkeypatch.setattr(app_mod, "load_settings", lambda: settings)

    # --apply web --channel email must fail cleanly
    assert app_mod.run(["--apply", "web", "--channel", "email", "--defaults"]) == 1

    # --apply web prepares a tailored Web/ATS pack for the email-less ATS job
    assert app_mod.run(["--apply", "web", "--defaults", "--no-ai", "--cv", "Andi_CV.pdf"]) == 0
    db = Database(settings.database_path)
    try:
        tr = ApplicationTracker(db)
        pending = tr.list([S.PENDING_APPROVAL])
        assert len(pending) == 1
        app_id = pending[0].id
        assert pending[0].channel == "web" and pending[0].mode == "ai"
        assert pending[0].generated_cv_path and Path(pending[0].generated_cv_path).exists()
    finally:
        db.close()

    # Lifecycle CLI updates: applied_web -> interview -> filter --applications --status INTERVIEW
    assert app_mod.run(["--update-status", app_id, "applied_web", "--note", "Submitted via Ashby", "--defaults"]) == 0
    assert app_mod.run(["--update-status", app_id, "interview", "--note", "HR call Friday", "--defaults"]) == 0
    assert app_mod.run(["--applications", "--status", "INTERVIEW", "--defaults"]) == 0

    db = Database(settings.database_path)
    try:
        rec = ApplicationTracker(db).get(app_id)
        assert rec is not None and rec.status == "INTERVIEW"
        assert any("HR call Friday" in n for n in rec.notes)
    finally:
        db.close()


def test_interactive_web_review_marks_applied_web(settings, tmp_path: Path, monkeypatch) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(apply_cli, "open_file", lambda *a, **k: None)
    monkeypatch.setattr(apply_cli, "open_folder", lambda *a, **k: None)
    monkeypatch.setattr(apply_cli, "open_url", lambda *a, **k: None)

    db = Database(tmp_path / "rev.sqlite3")
    try:
        tr = ApplicationTracker(db)
        rec = tr.create(
            mode="ai", channel="web", job_key="jw1", company_name="Xendit", job_title="Backend Engineer",
            job_url="https://boards.greenhouse.io/xendit/1", email_subject="Cover Note",
            email_body="Applying for Backend Engineer at Xendit.", status="PENDING_APPROVAL",
        )
        # Menu for AI web record without attachment:
        # 0 Open URL, 1 Edit cover note, 2 Regenerate CV, 3 Mark as Applied (APPLIED_WEB), ...
        DOWN = "\x1b[B"
        with create_pipe_input() as inp:
            inp.send_text(DOWN * 3 + "\r")
            with create_app_session(input=inp, output=DummyOutput()):
                asyncio.run(apply_cli.review([rec], ApplicationManager(settings, db), None, dry_run=True, interactive=True))
        after = tr.get(rec.id)
        assert after is not None and after.status == "APPLIED_WEB" and after.sent_at
    finally:
        db.close()


def test_hub_snapshot_filter_jobs_and_exit(settings, tmp_path: Path) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    db = Database(settings.database_path)
    try:
        repo = Repository(db)
        repo.create_session("s1", None, SearchConfig())
        jobs = [
            JobOpportunity(
                job_id="a1", job_title="Backend Engineer", company_name="Xendit", country="Indonesia",
                relevance_score=88.0, relevance_category="HIGHLY RELEVANT", verification_status="VERIFIED_OPEN",
                application_url="https://boards.greenhouse.io/xendit/1", required_skills=["Node.js", "PostgreSQL"],
            ),
            JobOpportunity(
                job_id="a2", job_title="Frontend Engineer", company_name="Startup", country="Singapore",
                relevance_score=65.0, relevance_category="RELEVANT", verification_status="LIKELY_OPEN",
                application_emails=["jobs@startup.sg"], required_skills=["React"],
            ),
        ]
        repo.save_opportunities("s1", jobs)
        repo.finish_session("s1", "completed", {"unique_opportunities": 2}, None)

        snap = hub_snapshot(settings, db)
        assert snap.latest_session_id == "s1"
        assert snap.latest_unique == 2 and snap.latest_email_jobs == 1 and snap.latest_web_jobs == 1
        assert snap.sending_badge == "DRY RUN"

        assert [j.job_id for j in filter_jobs(jobs, channel="EMAIL")] == ["a2"]
        assert [j.job_id for j in filter_jobs(jobs, country="Indonesia")] == ["a1"]
        assert [j.job_id for j in filter_jobs(jobs, keyword="PostgreSQL")] == ["a1"]
    finally:
        db.close()

    # Drive InteractiveHub to option 8 (Exit) via simulated keystrokes
    app = app_mod.Application(app_mod.parse_args([]))
    app.settings = settings
    app.interactive = True
    DOWN = "\x1b[B"
    with create_pipe_input() as inp:
        inp.send_text(DOWN * 7 + "\r")
        with create_app_session(input=inp, output=DummyOutput()):
            assert InteractiveHub(app).run() == 0
