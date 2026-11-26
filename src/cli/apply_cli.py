"""CLI for application automation: prepare (Basic / AI), review and approve, send, history, safety controls."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table

from src.ai.provider_manager import AIManager
from src.applications.email_service import (
    AUTH_PAUSE_PREFIX,
    AttachmentError,
    DryRunTransport,
    EmailService,
    GmailTransport,
    MailTransport,
    SmtpTransport,
    TestRedirectTransport,
    attachment_name,
    transport_kind,
)
from src.applications.smtp_client import SmtpClient
from src.applications.gmail_client import GmailAuth, GmailAuthError, GmailClient
from src.applications.manager import ApplicationManager, PrepareSummary
from src.applications.models import ApplicationMode, ApplicationRecord, ApplicationStatus
from src.applications.scheduler import SendingScheduler
from src.applications.sending_limits import SendingGuard, SendingLimits
from src.applications.setup_check import setup_checklist, validate_applications_settings
from src.applications.tracker import ApplicationTracker, DuplicateApplicationError, InvalidTransitionError
from src.cli import display as ui
from src.cli.questions import Cancelled
from src.core.configuration import Settings
from src.core.exceptions import JobIntelError
from src.core.orchestrator import build_http
from src.cv.analyzer import CVAnalyzer
from src.cv.models import CandidateProfile, ParsedCV
from src.cv.parser import list_cv_files
from src.storage.database import Database
from src.storage.repository import Repository
from src.utils.date_time import local_now

S = ApplicationStatus


# ------------------------------------------------------------------ runtime
def is_dry_run(settings: Settings, force_dry_run: bool) -> bool:
    return force_dry_run or bool(settings.get("applications.sending.dry_run", True))


def test_recipient(settings: Settings) -> str:
    """Optional simulation address: every real e-mail is redirected there (employers receive nothing)."""
    return (os.environ.get("APPLICATION_TEST_RECIPIENT", "").strip()
            or str(settings.get("applications.sending.test_recipient", "") or "").strip())


def test_tag_subject(settings: Settings) -> bool:
    """Whether TestRedirectTransport prefixes subjects with '[TEST for ...]'. Defaults to config."""
    raw = os.environ.get("APPLICATION_TEST_TAG_SUBJECT", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return bool(settings.get("applications.sending.test_tag_subject", False))


def build_transport(settings: Settings, dry_run: bool) -> tuple[MailTransport, str]:
    transport, sender = _build_base_transport(settings, dry_run)
    test = test_recipient(settings)
    if test and not dry_run:
        from src.applications.contacts import validate_address

        ok, why = validate_address(test)
        if not ok:
            raise JobIntelError(f"APPLICATION_TEST_RECIPIENT '{test}' is not usable: {why}")
        transport = TestRedirectTransport(transport, test, tag_subject=test_tag_subject(settings))
    return transport, sender


def _build_base_transport(settings: Settings, dry_run: bool) -> tuple[MailTransport, str]:
    if dry_run:
        outbox = settings.path("applications.sending.outbox_directory", "data/outbox")
        sender = os.environ.get("GMAIL_SENDER_ADDRESS", "").strip() or "dry-run@localhost.invalid"
        return DryRunTransport(outbox), sender
    sender = os.environ.get("GMAIL_SENDER_ADDRESS", "").strip()
    if not sender:
        raise JobIntelError("GMAIL_SENDER_ADDRESS is not set in .env (the Gmail address you send from).")
    s = settings.get("applications.sending", {}) or {}
    attempts, backoff = int(s.get("max_send_attempts", 3)), float(s.get("backoff_base_seconds", 2))
    if transport_kind(settings) == "smtp":
        client = SmtpClient(sender, os.environ.get("GMAIL_APP_PASSWORD", ""),
                            port=int(s.get("smtp_port", 587)), max_attempts=attempts, backoff_base=backoff)
        return SmtpTransport(client), sender
    auth = GmailAuth.from_settings(settings)
    if not auth.has_token():
        raise GmailAuthError("Gmail is not authorised yet. Run: python main.py --gmail-auth "
                             "(or use an App Password: GMAIL_APP_PASSWORD in .env)")
    client = GmailClient(lambda force: auth.access_token(force), max_attempts=attempts, backoff_base=backoff)
    return GmailTransport(client), sender


def build_service(settings: Settings, db: Database, *, dry_run: bool,
                  identity_name: str = "", own_email: str | None = None) -> EmailService:
    transport, sender = build_transport(settings, dry_run)
    tracker = ApplicationTracker(db, recipient_cooldown_days=float(settings.get("applications.recipient_cooldown_days", 30)))
    guard = SendingGuard.from_settings(settings, tracker, transport.name)
    applicant_email = os.environ.get("APPLICANT_EMAIL", "").strip()
    own = own_email or applicant_email or os.environ.get("GMAIL_SENDER_ADDRESS", "").strip()
    reply_to = applicant_email or None
    return EmailService(tracker, guard, transport, sender=sender,
                        sender_name=os.environ.get("APPLICANT_NAME", "").strip() or identity_name,
                        max_attachment_bytes=int(float(settings.get("applications.sending.max_attachment_mb", 10)) * 1048576),
                        own_email=own or None,
                        reply_to=reply_to)


def _confirm(question: str, default: bool = False) -> bool:
    """Synchronous confirm: only for code that runs outside an event loop."""
    import questionary

    ans = questionary.confirm(question, default=default).ask()
    if ans is None:
        raise Cancelled()
    return bool(ans)


async def _aconfirm(question: str, default: bool = False) -> bool:
    """Confirm inside a coroutine (questionary's .ask() cannot run inside a running event loop)."""
    import questionary

    ans = await questionary.confirm(question, default=default).ask_async()
    if ans is None:
        raise Cancelled()
    return bool(ans)


# ------------------------------------------------------------------ display
def show_limits_banner(settings: Settings, dry_run: bool) -> None:
    lim = SendingLimits.from_settings(settings)
    mode = "[bold yellow]DRY RUN[/] (e-mails are written to the outbox, nothing is sent)" if dry_run else \
        "[bold red]LIVE[/] (e-mails are sent through Gmail)"
    ui.info(f"Sending: {mode}")
    test = test_recipient(settings)
    if test and not dry_run:
        ui.warn(f"TEST MODE: every e-mail is redirected to {test}. Employers receive nothing, and the "
                "applications go back to review after the test send.")
    ui.info(f"Limits: {lim.max_per_day}/day, {lim.max_per_hour}/hour, >= {lim.min_interval_seconds}s apart, "
            f"{lim.max_per_recipient_domain_per_day}/recipient domain/day ({lim.account_type} account).")
    for w in lim.warnings():
        ui.warn(w)


def _pdf_preview(path: str | None, lines: int = 14) -> str:
    if not path or not Path(path).exists():
        return "(no file)"
    try:
        import pymupdf

        doc = pymupdf.open("pdf", Path(path).read_bytes())
        try:
            text = doc[0].get_text()
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        return f"(preview unavailable: {exc})"
    out = [l for l in text.splitlines() if l.strip()][:lines]
    return "\n".join(out)


def show_application(rec: ApplicationRecord, *, dry_run: bool, sender_name: str = "") -> None:
    is_web = rec.channel == "web"
    eff_sender = (rec.sender_name or "").strip() or sender_name
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", no_wrap=True)
    t.add_column(overflow="fold")
    chan_label = "WEB / ATS ASSIST" if is_web else "E-MAIL"
    t.add_row("Application", f"{rec.id}  ({chan_label} · {rec.mode.upper()} mode, status {rec.status})")
    t.add_row("Company", rec.company_name or "[yellow]unknown[/]")
    t.add_row("Job title", rec.job_title or "")
    t.add_row("Apply URL" if is_web else "Job URL", rec.job_url or "")
    if not is_web:
        t.add_row("Recipient", rec.recipient_email or "[red]none[/]")
    t.add_row("Match score", f"{rec.match_score:g}" if rec.match_score is not None else "n/a")
    m = rec.match or {}
    match = m.get("match") or {}
    if match:
        comps = ", ".join(f"{k} {v:.2f} (w{match.get('weights', {}).get(k, 0):g})" for k, v in match.get("components", {}).items())
        t.add_row("Score components", comps)
        t.add_row("Matching skills", ", ".join(f"{x['skill']}" for x in match.get("matched_skills", [])) or "-")
        t.add_row("Missing skills", ", ".join(match.get("missing_skills", [])) or "-")
        rel = "; ".join(f"{x['title']} ({', '.join(x['matched_terms'][:4])})" for x in match.get("relevant_experience", [])[:3])
        t.add_row("Relevant experience", rel or "-")
        if match.get("relevant_projects"):
            t.add_row("Relevant projects", "; ".join(match["relevant_projects"][:3]))
        t.add_row("Explanation", "\n".join(match.get("explanation", [])))
        if match.get("concerns"):
            t.add_row("[yellow]Concerns[/]", "\n".join(match["concerns"]))
    elif m.get("explanation") or m.get("relevance_category"):
        t.add_row("Relevance", f"{m.get('relevance_category', '')}: " + " ".join(m.get("explanation", [])[:3]))
        if m.get("eligibility"):
            t.add_row("Eligibility", "; ".join(m["eligibility"]))
        if m.get("verification_status"):
            t.add_row("Posting status", m["verification_status"])
    analysis = m.get("analysis") or {}
    reqs = m.get("requirements") or analysis.get("requirements") or []
    resp = m.get("responsibilities") or analysis.get("responsibilities") or []
    if resp:
        t.add_row("Responsibilities", "\n".join(f"- {x}" for x in resp[:4]))
    if reqs:
        t.add_row("Requirements", "\n".join(f"- {x}" for x in reqs[:6]))
    elif m.get("description_excerpt"):
        t.add_row("Description", m["description_excerpt"] + ("..." if len(m["description_excerpt"]) >= 400 else ""))
    if m.get("cv_changes"):
        t.add_row("CV changes", "\n".join(m["cv_changes"]))
    if m.get("rejected_rewrites"):
        t.add_row("Rejected AI rewrites", f"{len(m['rejected_rewrites'])} (failed the fact check; original wording kept)")
    t.add_row("CV file" if is_web else "CV attached", rec.attachment_path or "[red]none[/]")
    if not is_web:
        t.add_row("Attachment name", attachment_name(rec, eff_sender))
    t.add_row("Note method" if is_web else "E-mail method", str(m.get("email_method", "template")))
    if rec.notes:
        t.add_row("[yellow]Notes[/]", "\n".join(rec.notes))
    ui.console.print(Panel(t, title=f"Review {rec.id}", border_style="cyan"))
    if is_web:
        ui.console.print(Panel(rec.email_body or "(no cover note)", title="Cover Note (paste into web/ATS form)",
                               border_style="green"))
    else:
        ui.console.print(Panel(f"[bold]Subject:[/] {rec.email_subject}\n\n{rec.email_body}", title="E-mail",
                               border_style="green"))
    if rec.mode == ApplicationMode.AI.value and rec.generated_cv_path:
        ui.console.print(Panel(_pdf_preview(rec.generated_cv_path), title="Generated CV preview (page 1)",
                               border_style="magenta"))
    if is_web:
        ui.console.print("[dim]WEB/ATS ASSIST: open the link, upload the CV PDF, paste the cover note, then choose "
                         "'Mark as Applied (APPLIED_WEB)'.[/]")
    else:
        ui.console.print("[dim]" + ("DRY RUN: approving writes an .eml file, nothing is sent." if dry_run else
                                    "LIVE: approving and sending e-mails this employer from your Gmail account.") + "[/]")


def show_summary(summary: PrepareSummary) -> None:
    ui.section("PREPARATION SUMMARY")
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column(justify="right")
    t.add_row("Jobs considered", str(summary.considered))
    if summary.no_email:
        t.add_row("Without an application e-mail (use --apply web)", str(summary.no_email))
    if summary.no_url:
        t.add_row("Without an application URL", str(summary.no_url))
    if summary.previously_closed:
        t.add_row("Previously rejected/skipped (untouched)", str(summary.previously_closed))
    t.add_row("Invalid / unusable e-mail (SKIPPED)", str(len(summary.invalid_email)))
    t.add_row("Not eligible (REJECTED)", str(len(summary.rejected)))
    t.add_row("Already applied / in queue (duplicate)", str(len(summary.duplicates)))
    t.add_row("Failed during preparation", str(len(summary.failed)))
    t.add_row("[green]Ready for review (PENDING_APPROVAL)[/]", str(len(summary.prepared)))
    ui.console.print(t)
    for job, reasons in summary.rejected[:8]:
        ui.console.print(f"  [dim]- rejected: {job.job_title} @ {job.company_name or '?'}: {reasons[0]}[/]")
    for job, existing in summary.duplicates[:5]:
        if existing is not None:
            ui.console.print(f"  [dim]- duplicate: {job.job_title} @ {job.company_name or '?'} (existing {existing.id}, "
                             f"{existing.status})[/]")


# ------------------------------------------------------------------- review
async def review(records: list[ApplicationRecord], manager: ApplicationManager, scheduler: SendingScheduler | None,
                 *, dry_run: bool, interactive: bool) -> None:
    import questionary

    pending = [r for r in records if r.status == S.PENDING_APPROVAL.value]
    if not pending:
        ui.info("No applications are waiting for approval.")
        return
    if not interactive:
        ui.info(f"{len(pending)} application(s) wait for approval. Review them with: python main.py --review")
        return
    manual = str(manager.settings.get("applications.sending.mode", "manual")) == "manual"
    can_send_now = manual and scheduler is not None
    if scheduler is None and any(r.channel == "email" for r in pending):
        ui.warn("Sending is not set up yet (see python main.py --setup-check). You can still review and approve; "
                "approved applications wait in the queue for: python main.py --send-queue")
    sender_name = scheduler.service.sender_name if scheduler is not None else os.environ.get("APPLICANT_NAME", "")
    test_addr = test_recipient(manager.settings)
    for idx, rec in enumerate(pending, 1):
        while True:
            rec = manager.tracker.get(rec.id) or rec
            if rec.status != S.PENDING_APPROVAL.value:
                break
            ui.section(f"APPLICATION {idx} OF {len(pending)}")
            show_application(rec, dry_run=dry_run, sender_name=sender_name)
            if rec.channel == "web":
                choices = ["Open Application URL in browser"]
                if rec.attachment_path:
                    choices += ["Open CV PDF", "Open CV folder"]
                choices.append("Edit cover note")
                if rec.mode == ApplicationMode.AI.value:
                    choices.append("Regenerate CV")
                choices += [
                    "Mark as Applied (APPLIED_WEB)",
                    DECIDE_LATER,
                    "Reject (not suitable)",
                    "Skip (do not apply)",
                    "Stop reviewing",
                ]
            else:
                choices = (["Approve and send now"] if can_send_now else []) + \
                    ["Approve (queue for --send-queue)", "Edit e-mail"]
                if rec.mode == ApplicationMode.AI.value:
                    choices.append("Regenerate CV")
                if rec.attachment_path:
                    choices.append("Open CV PDF")
                choices += [DECIDE_LATER, "Reject (not suitable)", "Skip (do not apply)", "Stop reviewing"]
            action = await questionary.select("Action:", choices=choices, default=choices[0]).ask_async()
            if action is None or action == "Stop reviewing":
                return
            if action == DECIDE_LATER:
                ui.info("Kept as PENDING_APPROVAL. Review it later with: python main.py --review")
                break
            if action == "Open Application URL in browser":
                open_url(rec.job_url)
                continue
            if action == "Open CV folder":
                open_folder(rec.attachment_path)
                continue
            if action == "Mark as Applied (APPLIED_WEB)":
                manager.tracker.mark_applied_web(rec.id, note="marked applied in CLI review")
                ui.ok("Marked as APPLIED_WEB.")
                break
            if action.startswith("Approve"):
                if not dry_run:
                    test_note = f" [TEST redirect -> {test_addr}]" if test_addr else ""
                    prompt = (
                        f"Send this application to {rec.recipient_email} from your Gmail account{test_note}?"
                        if action == "Approve and send now"
                        else f"Queue this application for --send-queue (to {rec.recipient_email}{test_note})?"
                    )
                    if not await _aconfirm(prompt, default=False):
                        continue
                rec = manager.tracker.approve(rec.id, approved_by="user (CLI review)")
                ui.ok("Approved.")
                if action == "Approve and send now" and scheduler is not None:
                    ok, msg = await scheduler.send_one(rec.id)
                    (ui.ok if ok else ui.warn)(msg)
                else:
                    ui.info("Queued. Send approved applications with: python main.py --send-queue")
                break
            if action == "Open CV PDF":
                open_file(rec.attachment_path)
                continue
            if action in ("Edit e-mail", "Edit cover note"):
                edited = await edit_email_text(rec.email_subject, rec.email_body)
                if edited is None:
                    continue
                subject, body = edited
                if subject == rec.email_subject and body == rec.email_body:
                    ui.info("No changes made.")
                    continue
                problems, warnings = check_edited_email(subject, body, rec)
                if problems:
                    ui.error("Not saved: " + "; ".join(problems))
                    continue
                for w in warnings:
                    ui.warn(w)
                manager.edit_email(rec.id, subject, body)
                ui.ok("Updated. Review it again before continuing.")
                continue
            if action == "Regenerate CV":
                try:
                    await manager.regenerate_cv(rec.id)
                    ui.ok("CV regenerated.")
                except (JobIntelError, ValueError) as exc:
                    ui.error(str(exc))
                continue
            if action.startswith("Reject"):
                manager.tracker.reject(rec.id, "rejected by user in review", closed_by="user")
                ui.info("Rejected (this job will not be offered again).")
                break
            if action.startswith("Skip"):
                manager.tracker.skip(rec.id, "skipped by user in review", closed_by="user")
                ui.info("Skipped (this job will not be offered again).")
                break


DECIDE_LATER = "Decide later (keep pending)"
_PLACEHOLDER = re.compile(r"\[[^\]]{2,40}\]|\{[^}]{2,40}\}|<[^>]{2,30}>|lorem ipsum", re.I)
_EDIT_MARK = "----- Write the text below this line. Save and close the editor when done. -----"


def open_file(path: str | None) -> None:
    """Open a file with the system viewer (PDF reader)."""
    if not path or not Path(path).exists():
        ui.error(f"File not found: {path}")
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])  # noqa: S603
        ui.info(f"Opened {path}")
    except OSError as exc:
        ui.error(f"Cannot open {path}: {exc}")


def open_folder(path: str | None) -> None:
    """Open the containing folder of a file (for quick ATS upload drag-and-drop)."""
    if not path or not Path(path).exists():
        ui.error(f"File not found: {path}")
        return
    folder = str(Path(path).resolve().parent)
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])  # noqa: S603
        ui.info(f"Opened folder {folder}")
    except OSError as exc:
        ui.error(f"Cannot open folder {folder}: {exc}")


def open_url(url: str | None) -> None:
    """Open an application URL in the user's default browser."""
    if not url or not url.startswith(("http://", "https://")):
        ui.error(f"Invalid application URL: {url}")
        return
    webbrowser.open(url)
    ui.info(f"Opened {url}")


async def edit_email_text(subject: str, body: str) -> tuple[str, str] | None:
    """Edit subject+body in Notepad (Windows) / $EDITOR, or in the terminal as a fallback."""
    import questionary

    raw_editor = os.environ.get("EDITOR", "").strip()
    editor = ["notepad.exe"] if sys.platform == "win32" else (shlex.split(raw_editor) if raw_editor else [])
    if editor:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application_email.txt"
            path.write_text(f"Subject: {subject}\n{_EDIT_MARK}\n{body}\n", encoding="utf-8")
            ui.info("Opened in your editor. Save and close it to continue.")
            try:
                await asyncio.to_thread(subprocess.run, [*editor, str(path)], check=False)
                text = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                ui.warn(f"Editor not available ({exc}); editing in the terminal instead.")
            else:
                head, sep, new_body = text.partition(_EDIT_MARK)
                if not sep:
                    ui.error("The marker line was removed; changes ignored.")
                    return None
                new_subject = head.strip()
                new_subject = new_subject[len("Subject:"):].strip() if new_subject.lower().startswith("subject:") \
                    else new_subject
                return new_subject, new_body.strip()
    new_subject = await questionary.text("Subject:", default=subject).ask_async()
    new_body = await questionary.text("Body (Esc then Enter to finish):", default=body, multiline=True).ask_async()
    if new_subject is None or new_body is None:
        return None
    return new_subject.strip(), new_body.strip()


def check_edited_email(subject: str, body: str, rec: ApplicationRecord) -> tuple[list[str], list[str]]:
    """Problems block saving; warnings are shown."""
    problems: list[str] = []
    warnings: list[str] = []
    if not subject or len(subject) > 200 or "\n" in subject:
        problems.append("the subject must be one line of 1-200 characters")
    if len(body) < 30:
        problems.append("the body is too short (at least 30 characters)")
    if len(body) > 10000:
        problems.append("the body is too long (max 10,000 characters)")
    if _PLACEHOLDER.search(subject + " " + body):
        problems.append("it still contains a placeholder such as [Name] or {company}")
    text = f"{subject}\n{body}".lower()
    if rec.job_title and rec.job_title.lower() not in text:
        warnings.append(f"The e-mail no longer mentions the job title '{rec.job_title}'.")
    if rec.company_name and rec.company_name.lower() not in text:
        warnings.append(f"The e-mail does not mention the company '{rec.company_name}'.")
    return problems, warnings


# --------------------------------------------------------------- commands
class ApplyCommands:
    def __init__(self, app: Any) -> None:
        self.app = app  # src.cli.application.Application (already ran environment())
        self.settings: Settings = app.settings
        self.args = app.args

    def _db(self) -> Database:
        return Database(self.settings.database_path)

    async def _duplicate_authorization(self) -> tuple[bool, str | None]:
        if not getattr(self.args, "allow_duplicate", False):
            return False, None
        if not self.app.interactive:
            ui.error("--allow-duplicate needs an interactive confirmation; it is ignored in non-interactive mode.")
            return False, None
        if await _aconfirm("You asked to allow applying again to positions you already applied to. Confirm?",
                           default=False):
            return True, f"confirmed by user in CLI at {local_now(self.settings.timezone).isoformat(timespec='seconds')}"
        return False, None

    def _basic_pdf(self, cv: ParsedCV) -> Path:
        cv_dir = self.settings.cv_directory
        configured = str(self.settings.get("applications.basic_cv_file", "") or "")
        if configured:
            return cv_dir / configured
        chosen = cv_dir / cv.filename
        if chosen.suffix.lower() == ".pdf":
            return chosen
        pdfs = [p for p in list_cv_files(cv_dir) if p.suffix.lower() == ".pdf"]
        same = [p for p in pdfs if p.stem.lower() == chosen.stem.lower()]
        if same or len(pdfs) == 1:
            return (same or pdfs)[0]
        raise AttachmentError("Basic mode attaches your original CV as a PDF. Put a PDF version in cv/ or set "
                              "applications.basic_cv_file in config/settings.yaml.")

    async def _profile(self, db: Database, cv: ParsedCV, use_ai: bool) -> CandidateProfile:
        stored = Repository(db).latest_profile_for(cv.sha256)
        if stored is not None:
            if use_ai and stored.analysis_method == "deterministic":
                ui.info(f"Upgrading deterministic profile for {cv.filename} with Gemini...")
                profile = await self.app.build_profile(cv, True)
                Repository(db).save_profile(profile)
                return profile
            ui.ok(f"Using the stored candidate profile for {cv.filename} ({stored.analysis_method}).")
            return stored
        if use_ai:
            profile = await self.app.build_profile(cv, True)
        else:
            profile = CVAnalyzer(self.settings).analyze(cv)
        Repository(db).save_profile(profile)
        return profile

    def _session_config(self, db: Database, session_id: str | None) -> tuple[Any, Any]:
        from src.core.models import SearchConfig
        from src.discovery.query_generator import parse_preferences

        if session_id and not session_id.startswith("recent-"):
            sess = Repository(db).get_session(session_id)
            if sess and sess.get("config_json"):
                try:
                    cfg = SearchConfig.from_dict(json.loads(sess["config_json"]))
                    return cfg, parse_preferences(cfg.additional_preferences)
                except (ValueError, TypeError):
                    pass
        return None, None

    def _scheduler(self, db: Database, dry_run: bool, identity_name: str = "",
                   own_email: str | None = None) -> SendingScheduler | None:
        """The send path, or None (with a clear message) when sending is not set up yet."""
        try:
            service = build_service(self.settings, db, dry_run=dry_run, identity_name=identity_name,
                                    own_email=own_email)
        except JobIntelError as exc:
            ui.warn(f"Sending is not set up: {exc}")
            return None
        return SendingScheduler(service, service.tracker, on_event=ui.info,
                                max_wait_seconds=float(self.settings.get("applications.sending.max_wait_seconds", 900)))

    @staticmethod
    def resolve_mode_channel(mode_arg: str, channel_arg: str | None) -> tuple[ApplicationMode, str]:
        """Resolve CLI --apply + --channel into orthogonal (ApplicationMode, channel)."""
        if mode_arg == "web":
            if channel_arg == "email":
                raise JobIntelError("--apply web cannot be combined with --channel email.")
            return ApplicationMode.AI, "web"
        return ApplicationMode(mode_arg), (channel_arg or "email")

    async def apply(self, mode_arg: str, *, channel_arg: str | None = None,
                    all_recent_days: int | None = None, limit_override: int | None = None,
                    jobs_override: list[Any] | None = None) -> int:
        validate_applications_settings(self.settings)
        mode, channel = self.resolve_mode_channel(
            mode_arg, channel_arg if channel_arg is not None else getattr(self.args, "channel", None)
        )
        dry_run = is_dry_run(self.settings, getattr(self.args, "dry_run", False))
        ui.section(f"APPLICATION AUTOMATION - {mode.value.upper()} MODE ({channel.upper()} CHANNEL)")
        if channel in ("email", "all"):
            show_limits_banner(self.settings, dry_run)
        cv = await asyncio.to_thread(self.app.choose_cv)
        if cv is None:
            return 1
        use_ai = mode == ApplicationMode.AI and await asyncio.to_thread(self.app.ai_consent)
        if mode == ApplicationMode.AI and not use_ai:
            ui.warn("AI is not available or not consented: analysis is deterministic and the CV is only reordered.")
        allow_dup, authorization = await self._duplicate_authorization()
        db = self._db()
        http = build_http(self.settings, timeout=float(self.settings.get("ai.request_timeout_seconds", 90))) if use_ai else None
        try:
            ai = None
            if use_ai:
                ai = AIManager.from_settings(self.settings, http)
                ai.max_calls = int(self.settings.get("applications.ai.max_calls_per_run", 60))
            profile = await self._profile(db, cv, use_ai)
            recent_days = all_recent_days if all_recent_days is not None else getattr(self.args, "all_recent", None)
            tmp_mgr = ApplicationManager(self.settings, db, ai=ai)
            if jobs_override is not None:
                session_id, jobs = getattr(self.args, "session", None), list(jobs_override)
            else:
                try:
                    session_id, jobs = tmp_mgr.load_jobs(getattr(self.args, "session", None), all_recent_days=recent_days)
                except ValueError as exc:
                    ui.error(str(exc))
                    return 1
            if not jobs:
                ui.error("No scraped jobs found. Run a search first: python main.py")
                return 1
            sess_cfg, sess_prefs = self._session_config(db, session_id)
            manager = ApplicationManager(self.settings, db, ai=ai, config=sess_cfg, preferences=sess_prefs)
            cv_path = self.settings.cv_directory / cv.filename
            await manager.set_candidate(cv, profile, cv_path, use_ai=use_ai)
            with_email = sum(1 for j in jobs if j.application_emails)
            with_url = sum(1 for j in jobs if (j.application_url or j.source_url))
            ui.ok(f"Loaded {len(jobs)} job(s) ({session_id or 'selected'}): "
                  f"{with_email} e-mail, {with_url} web/ATS.")
            limit = limit_override if limit_override is not None else getattr(self.args, "limit", None)
            cv_pdf = self._basic_pdf(cv) if mode == ApplicationMode.BASIC else None
            summary = await manager.prepare_batch(
                jobs,
                mode=mode,
                channel=channel,
                cv_pdf=cv_pdf,
                session_id=session_id,
                limit=limit,
                allow_duplicate=allow_dup,
                authorization=authorization,
                on_progress=ui.info,
            )
            show_summary(summary)
            if ai is not None:
                ui.info(f"AI usage: {ai.status_line()}")
            if self.app.prefs is not None:
                self.app.prefs.save(self.app.saved)
            scheduler = (
                self._scheduler(db, dry_run, manager.doc.identity.name, manager.doc.identity.email)
                if channel in ("email", "all")
                else None
            )
            # This run's applications first, then older ones still waiting for a decision.
            new_ids = {r.id for r in summary.prepared}
            older = [
                r for r in manager.tracker.list([S.PENDING_APPROVAL], channel=None if channel == "all" else channel)
                if r.id not in new_ids
            ]
            if older:
                ui.info(f"{len(older)} older application(s) are also waiting for review; they follow this run's.")
            await review(summary.prepared + older, manager, scheduler, dry_run=dry_run,
                         interactive=self.app.interactive)
            if scheduler is not None:
                await self._maybe_automatic(manager, scheduler, summary.prepared)
            return 0
        finally:
            if http is not None:
                await http.aclose()
            db.close()

    async def _maybe_automatic(self, manager: ApplicationManager, scheduler: SendingScheduler,
                               records: list[ApplicationRecord]) -> None:
        if not getattr(self.args, "auto", False):
            return
        ok, why = manager.automatic_allowed(True)
        if not ok:
            ui.warn(f"Automatic sending not enabled: {why}.")
            return
        email_records = [r for r in records if r.channel == "email"]
        if not email_records:
            return
        approved = manager.auto_approve(email_records)
        ui.info(f"Automatic mode: {len(approved)} application(s) approved by policy.")
        report = await scheduler.process_queue()
        self._show_queue_report(report)

    # ----------------------------------------------------------- utilities
    async def review_pending(self, *, channel: str | None = None) -> int:
        validate_applications_settings(self.settings)
        dry_run = is_dry_run(self.settings, getattr(self.args, "dry_run", False))
        db = self._db()
        http = None
        try:
            tracker = ApplicationTracker(db)
            pending = tracker.list([S.PENDING_APPROVAL], channel=channel)
            needs_cv = any(r.mode == ApplicationMode.AI.value for r in pending)
            ai = None
            use_ai = False
            cv = None
            cv_path: Path | None = None
            if needs_cv:
                # Prefer the original CV recorded on the pending application so Regenerate CV uses the right source.
                for r in pending:
                    if r.original_cv_path and Path(r.original_cv_path).is_file():
                        from src.cv.parser import parse_cv

                        try:
                            cv = parse_cv(Path(r.original_cv_path))
                            cv_path = Path(r.original_cv_path)
                            break
                        except Exception:  # noqa: BLE001
                            pass
                if cv is None:
                    cv = await asyncio.to_thread(self.app.choose_cv)
                    if cv is not None:
                        cv_path = self.settings.cv_directory / cv.filename
                if cv is not None:
                    use_ai = await asyncio.to_thread(self.app.ai_consent)
                    if use_ai:
                        http = build_http(self.settings,
                                          timeout=float(self.settings.get("ai.request_timeout_seconds", 90)))
                        ai = AIManager.from_settings(self.settings, http)
                        ai.max_calls = int(self.settings.get("applications.ai.max_calls_per_run", 60))
            manager = ApplicationManager(self.settings, db, ai=ai)
            if cv is not None and cv_path is not None:
                profile = await self._profile(db, cv, use_ai)
                await manager.set_candidate(cv, profile, cv_path, use_ai=use_ai)
            if any(r.channel == "email" for r in pending):
                show_limits_banner(self.settings, dry_run)
                scheduler = self._scheduler(db, dry_run, manager.doc.identity.name if manager.doc else "",
                                            manager.doc.identity.email if manager.doc else None)
            else:
                scheduler = None
            await review(pending, manager, scheduler, dry_run=dry_run, interactive=self.app.interactive)
            return 0
        finally:
            if http is not None:
                await http.aclose()
            db.close()

    def setup_check(self) -> int:
        """Show what is set up and what the user still has to do manually."""
        db = self._db()
        try:
            _, jobs = ApplicationManager(self.settings, db).load_jobs()
        except ValueError:
            jobs = []
        finally:
            db.close()
        items = setup_checklist(self.settings, sum(1 for j in jobs if j.application_emails))
        t = Table(title="Application automation setup")
        for col in ("", "Item", "Needed for", "Status", "How to fix"):
            t.add_column(col, overflow="fold")
        needed = {"all": "all modes", "live": "real sending", "ai": "AI mode"}
        for i in items:
            t.add_row("[green]OK[/]" if i.ok else "[yellow]TODO[/]", i.name, needed.get(i.required_for, ""),
                      i.detail, "" if i.ok else i.fix)
        ui.console.print(t)
        blocking = [i for i in items if not i.ok and i.required_for == "all"]
        ui.info("Basic/AI preparation works in dry run once the 'all modes' items are OK; real sending also needs "
                "the 'real sending' items.")
        return 1 if blocking else 0

    async def send_queue(self) -> int:
        validate_applications_settings(self.settings)
        dry_run = is_dry_run(self.settings, self.args.dry_run)
        db = self._db()
        try:
            show_limits_banner(self.settings, dry_run)
            service = build_service(self.settings, db, dry_run=dry_run)
            scheduler = SendingScheduler(service, service.tracker, on_event=ui.info,
                                         max_wait_seconds=float(self.settings.get("applications.sending.max_wait_seconds", 900)))
            queued = service.tracker.list([S.APPROVED])
            ui.info(f"{len(queued)} approved application(s) in the queue.")
            if not dry_run and queued and self.app.interactive and not await _aconfirm(
                    f"Send up to {len(queued)} approved application(s) through Gmail now?", default=False):
                return 0
            report = await scheduler.process_queue(max_sends=self.args.limit)
            self._show_queue_report(report)
            return 0 if not report.failed else 1
        finally:
            db.close()

    @staticmethod
    def _show_queue_report(report: Any) -> None:
        real = [r for r in report.sent if r.status == S.SENT.value]
        tests = [r for r in report.sent if r.transport and "test->" in r.transport]
        dry = len(report.sent) - len(real) - len(tests)
        ui.ok(f"Sent to employers through Gmail: {len(real)}")
        if tests:
            ui.ok(f"TEST: {len(tests)} e-mail(s) sent to the test address {tests[0].transport.split('test->', 1)[1]} "
                  "(employers received nothing; the applications are back in review).")
        if dry:
            ui.info(f"Dry run: {dry} e-mail(s) written to the outbox (nothing sent). They are back in review; "
                    "approve them again with dry_run off to send for real.")
        for app_id, err in report.failed:
            ui.error(f"{app_id}: {err}")
        for app_id, why in report.refused:
            ui.warn(f"{app_id}: {why}")
        if report.stopped_reason:
            ui.info(f"Stopped: {report.stopped_reason}")
        ui.info(f"Still queued: {report.remaining}")

    def gmail_auth(self) -> int:
        auth = GmailAuth.from_settings(self.settings)
        ui.info(f"OAuth client file: {auth.client_secret_file}")
        ui.info(f"Token will be stored in: {auth.token_file} (git-ignored; keep it private)")
        ui.info("Requested scope: gmail.send only (send e-mail on your behalf; no reading of your mailbox).")
        auth.authorize_interactive(open_browser=True)
        ui.ok("Gmail authorised.")
        db = self._db()
        try:
            guard = SendingGuard.from_settings(self.settings, ApplicationTracker(db), "gmail")
            pause = guard.pause_state()
            if pause and str(pause.get("reason", "")).startswith(AUTH_PAUSE_PREFIX):
                guard.resume()
                ui.ok("The pause caused by the expired Gmail authorisation was cleared.")
        finally:
            db.close()
        return 0

    def _guard(self, db: Database) -> SendingGuard:
        name = "dry_run" if is_dry_run(self.settings, self.args.dry_run) else "gmail"
        return SendingGuard.from_settings(self.settings, ApplicationTracker(db), name)

    def emergency_stop(self) -> int:
        db = self._db()
        try:
            self._guard(db).emergency_stop("emergency stop from CLI")
            stop = self.settings.path("applications.sending.emergency_stop_file", "data/STOP_SENDING")
            stop.parent.mkdir(parents=True, exist_ok=True)
            stop.write_text(f"Emergency stop set at {local_now(self.settings.timezone).isoformat()}\n", encoding="utf-8")
            ui.ok(f"Emergency stop active. All sending is blocked until: python main.py --resume-sending")
            return 0
        finally:
            db.close()

    def resume_sending(self) -> int:
        db = self._db()
        try:
            guard = self._guard(db)
            guard.clear_emergency_stop()
            guard.resume()
            ui.ok("Emergency stop and pauses cleared. The daily/hourly limits still apply.")
            return 0
        finally:
            db.close()

    def sending_status(self) -> int:
        db = self._db()
        try:
            st = self._guard(db).status()
            ui.console.print_json(json.dumps(st, default=str))
            ui.console.print("Applications by status: " + json.dumps(ApplicationTracker(db).counts()))
            return 0
        finally:
            db.close()

    def list_applications(self, status_filter: str | None = None) -> int:
        db = self._db()
        try:
            tracker = ApplicationTracker(db)
            st_arg = status_filter if status_filter is not None else getattr(self.args, "status", None)
            statuses = [st_arg.upper()] if st_arg else None
            recs = tracker.list(statuses=statuses, limit=100)
            counts = tracker.counts()
        finally:
            db.close()
        title = f"Application history ({st_arg.upper()})" if st_arg else "Application history (latest 100)"
        t = Table(title=title)
        for col in ("ID", "Created", "Channel", "Mode", "Status", "Score", "Company", "Job title",
                    "Recipient / URL", "Sent / Applied", "Error"):
            t.add_column(col, overflow="fold")
        for r in recs:
            err = (r.error_kind or "") + (f": {r.error[:60]}" if r.error else "")
            if r.send_lock:
                err = "LOCKED - delivery unknown; check Gmail Sent, then --requeue " + r.id + (f" ({err})" if err else "")
            target = r.recipient_email or (r.job_url or "")[:45]
            t.add_row(r.id, (r.created_at or "")[:16], r.channel, r.mode, r.status,
                      f"{r.match_score:g}" if r.match_score is not None else "",
                      r.company_name or "", r.job_title or "", target, (r.sent_at or "")[:16], err)
        ui.console.print(t)
        if counts:
            funnel = [
                f"Pending {counts.get('PENDING_APPROVAL', 0)}",
                f"Queued {counts.get('APPROVED', 0)}",
                f"Sent(email) {counts.get('SENT', 0)}",
                f"Applied(web) {counts.get('APPLIED_WEB', 0)}",
                f"Interview {counts.get('INTERVIEW', 0)}",
                f"Offer {counts.get('OFFER', 0)}",
            ]
            ui.info("Pipeline: " + " · ".join(funnel))
            ui.info("Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0

    def update_status(self, app_id: str, new_status: str, note: str = "") -> int:
        valid = {"applied_web", "interview", "offer", "employer_rejected", "withdrawn"}
        norm = (new_status or "").strip().lower()
        if norm not in valid:
            ui.error(f"Unsupported status '{new_status}'. Choose one of: {', '.join(sorted(valid))}")
            return 1
        db = self._db()
        try:
            tracker = ApplicationTracker(db)
            rec = tracker.update_pipeline_status(app_id, norm.upper(), note=note)
            ui.ok(f"Application {rec.id} ({rec.job_title} @ {rec.company_name or '?'}) updated to {rec.status}.")
            return 0
        except (InvalidTransitionError, ValueError) as exc:
            ui.error(str(exc))
            return 1
        finally:
            db.close()

    def requeue(self, app_id: str) -> int:
        db = self._db()
        try:
            tracker = ApplicationTracker(db)
            rec = tracker.get(app_id)
            if rec is None:
                ui.error(f"Unknown application {app_id}")
                return 1
            if rec.error_kind == "delivery_unknown" or rec.send_lock:
                ui.warn("The previous attempt may have been delivered. Check the Gmail 'Sent' folder for "
                        f"Message-ID {rec.message_id_header or '(unknown)'} before sending again.")
                if not self.app.interactive or not _confirm("I checked: it was NOT sent. Re-queue for approval?"):
                    return 1
            tracker.requeue(app_id)
            ui.ok(f"{app_id} is back in PENDING_APPROVAL. Review it with: python main.py --review")
            return 0
        except InvalidTransitionError as exc:
            ui.error(str(exc))
            return 1
        finally:
            db.close()


def run_apply_command(app: Any) -> int | None:
    """Dispatch application-automation flags. Returns None when no such flag was given."""
    a = app.args
    cmd = ApplyCommands(app)
    if a.setup_check:
        return cmd.setup_check()
    if a.gmail_auth:
        return cmd.gmail_auth()
    if a.emergency_stop:
        return cmd.emergency_stop()
    if a.resume_sending:
        return cmd.resume_sending()
    if a.sending_status:
        return cmd.sending_status()
    if getattr(a, "update_status", None):
        app_id, st = a.update_status
        return cmd.update_status(app_id, st, note=getattr(a, "note", "") or "")
    if a.applications:
        return cmd.list_applications()
    if a.requeue:
        return cmd.requeue(a.requeue)
    if a.apply:
        return asyncio.run(cmd.apply(a.apply))
    if a.review:
        return asyncio.run(cmd.review_pending(channel=getattr(a, "channel", None) if getattr(a, "channel", None) in ("email", "web") else None))
    if a.send_queue:
        return asyncio.run(cmd.send_queue())
    return None
