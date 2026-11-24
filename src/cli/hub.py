"""Single-door Interactive Hub launched by flag-free interactive ``python main.py``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from rich.panel import Panel
from rich.table import Table

from src.applications.sending_limits import SendingGuard
from src.applications.tracker import ApplicationTracker
from src.cli import display as ui
from src.cli.apply_cli import ApplyCommands, is_dry_run, open_url, test_recipient
from src.cli.questions import Cancelled
from src.core.configuration import Settings
from src.core.models import JobOpportunity
from src.reporting.report_models import apply_channel_label
from src.storage.database import Database
from src.storage.repository import Repository
from src.utils.date_time import format_local, from_iso


@dataclass
class HubSnapshot:
    latest_session_id: str | None = None
    latest_session_date: str = "-"
    latest_unique: int = 0
    latest_email_jobs: int = 0
    latest_web_jobs: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    sending_badge: str = "DRY RUN"
    sending_alert: str = ""


def hub_snapshot(settings: Settings, db: Database) -> HubSnapshot:
    """Compute a live status snapshot from SQLite & Settings for the Hub header."""
    repo = Repository(db)
    tracker = ApplicationTracker(db)
    snap = HubSnapshot(counts=tracker.counts())
    sessions = [s for s in repo.list_sessions(10) if s.get("status") != "running"]
    if sessions:
        latest = sessions[0]
        snap.latest_session_id = latest["id"]
        snap.latest_session_date = format_local(from_iso(latest["started_at"]), settings.timezone)
        jobs = repo.session_opportunities(latest["id"])
        snap.latest_unique = len(jobs)
        snap.latest_email_jobs = sum(1 for j in jobs if j.application_emails)
        snap.latest_web_jobs = sum(1 for j in jobs if (j.application_url or j.source_url))
    dry = is_dry_run(settings, False)
    test = test_recipient(settings)
    if dry:
        snap.sending_badge = "DRY RUN"
    elif test:
        snap.sending_badge = f"TEST -> {test}"
    else:
        snap.sending_badge = "LIVE"
    try:
        guard = SendingGuard.from_settings(settings, tracker, "dry_run" if dry else "gmail")
        st = guard.status()
        if st.get("emergency_stop"):
            snap.sending_alert = "EMERGENCY STOP ACTIVE"
        elif st.get("paused"):
            snap.sending_alert = f"PAUSED: {st['paused'].get('reason', '')[:60]}"
    except Exception:  # noqa: BLE001
        pass
    return snap


def filter_jobs(
    jobs: list[JobOpportunity],
    *,
    category: str = "ALL",
    channel: str = "ALL",
    country: str = "",
    keyword: str = "",
) -> list[JobOpportunity]:
    """Filter scraped jobs for the Interactive Hub Job Browser."""
    out: list[JobOpportunity] = []
    cat_u = (category or "ALL").upper()
    ch_u = (channel or "ALL").upper()
    c_low = (country or "").strip().lower()
    kw_low = (keyword or "").strip().lower()
    for j in jobs:
        if cat_u != "ALL" and (j.relevance_category or "").upper() != cat_u:
            continue
        if ch_u == "EMAIL" and not j.application_emails:
            continue
        if ch_u == "WEB" and not (j.application_url or j.source_url):
            continue
        if c_low and c_low not in f"{j.country or ''} {j.location or ''}".lower():
            continue
        if kw_low and kw_low not in f"{j.job_title} {j.company_name or ''} {' '.join(j.required_skills)}".lower():
            continue
        out.append(j)
    out.sort(key=lambda j: -(j.relevance_score or 0))
    return out


class InteractiveHub:
    """Single-door interactive menu uniting discovery, browsing, preparation, review, sending & tracking."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings: Settings = app.settings

    def _snapshot(self) -> HubSnapshot:
        db = Database(self.settings.database_path)
        try:
            return hub_snapshot(self.settings, db)
        finally:
            db.close()

    def _render_header(self, snap: HubSnapshot) -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column(overflow="fold")
        if snap.latest_session_id:
            t.add_row(
                "Latest Search",
                f"{snap.latest_session_date} · {snap.latest_unique} jobs "
                f"({snap.latest_email_jobs} e-mail, {snap.latest_web_jobs} web/ATS)",
            )
        else:
            t.add_row("Latest Search", "No completed searches yet")
        c = snap.counts
        t.add_row(
            "Pipeline",
            f"Pending [bold]{c.get('PENDING_APPROVAL', 0)}[/] · "
            f"Queued [bold]{c.get('APPROVED', 0)}[/] · "
            f"Sent(email) [bold]{c.get('SENT', 0)}[/] · "
            f"Applied(web) [bold]{c.get('APPLIED_WEB', 0)}[/] · "
            f"Interview [bold]{c.get('INTERVIEW', 0)}[/] · "
            f"Offer [bold]{c.get('OFFER', 0)}[/]",
        )
        badge_style = "yellow" if snap.sending_badge == "DRY RUN" else ("magenta" if "TEST" in snap.sending_badge else "red")
        status_txt = f"[bold {badge_style}]{snap.sending_badge}[/]"
        if snap.sending_alert:
            status_txt += f"  [bold red]! {snap.sending_alert}[/]"
        t.add_row("Sending Mode", status_txt)
        ui.console.print()
        ui.console.print(Panel(t, title="AI Job Intelligence Hub", border_style="cyan"))

    def run(self) -> int:
        import questionary
        from questionary import Choice

        cmd = ApplyCommands(self.app)
        while True:
            snap = self._snapshot()
            self._render_header(snap)
            n_pending = snap.counts.get("PENDING_APPROVAL", 0)
            n_queued = snap.counts.get("APPROVED", 0)
            choices = [
                Choice("1. Search Jobs (discover & generate reports)", "search"),
                Choice("2. Browse & Filter Scraped Jobs", "browse"),
                Choice("3. Prepare Applications (Web/ATS Assist or E-mail)", "prepare"),
                Choice(f"4. Review Pending Applications ({n_pending} pending)", "review"),
                Choice(f"5. Send Approved E-mail Queue ({n_queued} queued)", "send"),
                Choice("6. Application Pipeline & History", "pipeline"),
                Choice("7. Setup, Health & Safety Controls", "setup"),
                Choice("8. Exit", "exit"),
            ]
            try:
                pick = questionary.select("Choose an action:", choices=choices).ask()
            except KeyboardInterrupt:
                return 0
            if pick in (None, "exit"):
                return 0
            try:
                if pick == "search":
                    rc = asyncio.run(self.app.main_flow())
                    if rc == 0 and questionary.confirm("Prepare applications from these results now?", default=False).ask():
                        self._prepare_flow(cmd)
                elif pick == "browse":
                    self._browse_flow(cmd)
                elif pick == "prepare":
                    self._prepare_flow(cmd)
                elif pick == "review":
                    asyncio.run(cmd.review_pending())
                elif pick == "send":
                    asyncio.run(cmd.send_queue())
                elif pick == "pipeline":
                    self._pipeline_flow(cmd)
                elif pick == "setup":
                    self._setup_flow(cmd)
            except Cancelled:
                ui.info("Returned to Hub.")
            except Exception as exc:  # noqa: BLE001 - keep hub alive on recoverable errors
                ui.error(str(exc))

    def _prepare_flow(self, cmd: ApplyCommands) -> None:
        import questionary
        from questionary import Choice

        ch = questionary.select(
            "Target channel:",
            choices=[
                Choice("Web/ATS Assist (Tailored CV + Cover Note for web forms)", "web"),
                Choice("E-mail Automation (direct e-mail applications)", "email"),
                Choice("All Channels (E-mail first, fallback to Web/ATS)", "all"),
                Choice("Back", "back"),
            ],
        ).ask()
        if ch in (None, "back"):
            return
        mode = questionary.select(
            "CV & copy mode:",
            choices=[
                Choice("AI (Tailored ATS PDF CV + tailored copy)", "ai"),
                Choice("Basic (Original PDF CV + template copy)", "basic"),
            ],
        ).ask()
        if mode is None:
            return
        scope = questionary.select(
            "Job pool:",
            choices=[
                Choice("Latest search session", "latest"),
                Choice("All recent jobs (last 14 days)", "recent"),
            ],
        ).ask()
        if scope is None:
            return
        lim_raw = questionary.text("Max applications to prepare (Enter = default):", default="").ask()
        limit = int(lim_raw.strip()) if lim_raw and lim_raw.strip().isdigit() and int(lim_raw.strip()) > 0 else None
        asyncio.run(
            cmd.apply(
                mode,
                channel_arg=ch,
                all_recent_days=14 if scope == "recent" else None,
                limit_override=limit,
            )
        )

    def _browse_flow(self, cmd: ApplyCommands) -> None:
        import questionary
        from questionary import Choice

        db = Database(self.settings.database_path)
        try:
            repo = Repository(db)
            tracker = ApplicationTracker(db)
            sessions = [s for s in repo.list_sessions(10) if s.get("status") != "running"]
            if not sessions:
                ui.warn("No scraped jobs found yet. Run 'Search Jobs' first.")
                return
            jobs = repo.session_opportunities(sessions[0]["id"])
            statuses = tracker.latest_status_by_job()
        finally:
            db.close()

        cat = questionary.select(
            "Filter by relevance category:",
            choices=[Choice("All categories", "ALL"), Choice("HIGHLY RELEVANT", "HIGHLY RELEVANT"),
                     Choice("RELEVANT", "RELEVANT"), Choice("POTENTIALLY RELEVANT", "POTENTIALLY RELEVANT")],
        ).ask() or "ALL"
        ch = questionary.select(
            "Filter by apply channel:",
            choices=[Choice("All channels", "ALL"), Choice("E-mail applicable", "EMAIL"), Choice("Web / ATS", "WEB")],
        ).ask() or "ALL"
        country = questionary.text("Country filter (Enter = any):", default="").ask() or ""
        kw = questionary.text("Keyword / skill filter (Enter = any):", default="").ask() or ""
        filtered = filter_jobs(jobs, category=cat, channel=ch, country=country, keyword=kw)
        if not filtered:
            ui.warn("No jobs matched those filters.")
            return

        shown = filtered[:25]
        t = Table(title=f"Scraped Jobs ({len(filtered)} matched, showing top {len(shown)})")
        for col in ("#", "Score", "Channel", "Title", "Company", "Country", "Verification", "App Status"):
            t.add_column(col, overflow="fold")
        for i, j in enumerate(shown, 1):
            t.add_row(
                str(i),
                str(int(j.relevance_score or 0)),
                apply_channel_label(j),
                j.job_title[:42],
                (j.company_name or "-")[:22],
                (j.country or "-")[:15],
                j.verification_status,
                statuses.get(j.job_id, "-"),
            )
        ui.console.print(t)

        pick = questionary.text("Enter job # to inspect/prepare (or press Enter to go back):", default="").ask()
        if not pick or not pick.strip().isdigit():
            return
        idx = int(pick.strip())
        if not 1 <= idx <= len(shown):
            ui.warn("Invalid selection.")
            return
        job = shown[idx - 1]
        details = (
            f"[bold]{job.job_title}[/] @ [bold]{job.company_name or 'Unknown'}[/]\n"
            f"Location: {job.location or job.country or '-'} ({job.work_arrangement}) · Score: {int(job.relevance_score or 0)}/100\n"
            f"Verification: {job.verification_status}\n"
            f"Apply URL: {job.application_url or job.source_url}\n"
            f"E-mails: {', '.join(job.application_emails) or 'None'}\n"
            f"Matched skills: {', '.join(job.match.matched_skills) or '-'}\n"
            f"Missing skills: {', '.join(job.match.missing_skills) or '-'}\n\n"
            f"{(job.job_description or '')[:700]}"
        )
        ui.show_panel(details, title="Job Details")
        act = questionary.select(
            "Action for this job:",
            choices=[
                Choice("Prepare tailored application (Web/ATS Assist)", "prep_web"),
                Choice("Prepare tailored application (E-mail)", "prep_email"),
                Choice("Open URL in browser", "open"),
                Choice("Back", "back"),
            ],
        ).ask()
        if act == "open":
            open_url(job.application_url or job.source_url)
        elif act == "prep_web":
            asyncio.run(cmd.apply("web", jobs_override=[job], limit_override=1))
        elif act == "prep_email":
            asyncio.run(cmd.apply("ai", channel_arg="email", jobs_override=[job], limit_override=1))

    def _pipeline_flow(self, cmd: ApplyCommands) -> None:
        import questionary
        from questionary import Choice

        act = questionary.select(
            "Application Pipeline:",
            choices=[
                Choice("View all applications", "all"),
                Choice("Filter by status", "filter"),
                Choice("Update application stage (Interview / Offer / Rejected / Withdrawn)", "update"),
                Choice("Requeue a FAILED application", "requeue"),
                Choice("Back", "back"),
            ],
        ).ask()
        if act in (None, "back"):
            return
        if act == "all":
            cmd.list_applications()
        elif act == "filter":
            st = questionary.select(
                "Status:",
                choices=["PENDING_APPROVAL", "APPROVED", "SENT", "APPLIED_WEB", "INTERVIEW", "OFFER",
                         "EMPLOYER_REJECTED", "WITHDRAWN", "FAILED", "REJECTED", "SKIPPED"],
            ).ask()
            if st:
                cmd.list_applications(status_filter=st)
        elif act == "update":
            db = Database(self.settings.database_path)
            try:
                recs = ApplicationTracker(db).list(
                    statuses=["PENDING_APPROVAL", "SENT", "APPLIED_WEB", "INTERVIEW", "OFFER"], limit=40
                )
            finally:
                db.close()
            if not recs:
                ui.info("No active applications to update.")
                return
            app_id = questionary.select(
                "Select application:",
                choices=[
                    Choice(f"{r.id} · [{r.status}/{r.channel}] {r.job_title} @ {r.company_name or '?'}", r.id)
                    for r in recs
                ],
            ).ask()
            if not app_id:
                return
            new_st = questionary.select(
                "New stage:",
                choices=[
                    Choice("Interview", "interview"),
                    Choice("Offer", "offer"),
                    Choice("Employer Rejected", "employer_rejected"),
                    Choice("Withdrawn", "withdrawn"),
                    Choice("Applied (Web/ATS)", "applied_web"),
                ],
            ).ask()
            if not new_st:
                return
            note = questionary.text("Optional note (Enter to skip):", default="").ask() or ""
            cmd.update_status(app_id, new_st, note=note)
        elif act == "requeue":
            db = Database(self.settings.database_path)
            try:
                failed = ApplicationTracker(db).list(statuses=["FAILED", "APPROVED"], limit=30)
            finally:
                db.close()
            if not failed:
                ui.info("No FAILED or locked applications found.")
                return
            app_id = questionary.select(
                "Select application to requeue:",
                choices=[Choice(f"{r.id} · [{r.status}] {r.job_title} @ {r.company_name or '?'}", r.id) for r in failed],
            ).ask()
            if app_id:
                cmd.requeue(app_id)

    def _setup_flow(self, cmd: ApplyCommands) -> None:
        import questionary
        from questionary import Choice

        act = questionary.select(
            "Setup, Health & Safety Controls:",
            choices=[
                Choice("Setup Check (--setup-check)", "setup_check"),
                Choice("Source Health Check (--check)", "check"),
                Choice("Sending Status (--sending-status)", "status"),
                Choice("Gmail OAuth Authorisation (--gmail-auth)", "gmail_auth"),
                Choice("Emergency Stop (--emergency-stop)", "stop"),
                Choice("Resume Sending (--resume-sending)", "resume"),
                Choice("Search History & Regenerate Reports", "history"),
                Choice("Back", "back"),
            ],
        ).ask()
        if act in (None, "back"):
            return
        if act == "setup_check":
            cmd.setup_check()
        elif act == "check":
            asyncio.run(self.app.check_sources())
        elif act == "status":
            cmd.sending_status()
        elif act == "gmail_auth":
            cmd.gmail_auth()
        elif act == "stop":
            cmd.emergency_stop()
        elif act == "resume":
            cmd.resume_sending()
        elif act == "history":
            self.app.show_history()
            sid = questionary.text("Enter Session ID to regenerate reports (or Enter to skip):", default="").ask()
            if sid and sid.strip():
                self.app.regenerate(sid.strip())
