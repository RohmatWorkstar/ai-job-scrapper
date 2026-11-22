"""Interactive CLI application: environment checks -> CV -> profile -> questions -> run -> reports."""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys
from pathlib import Path
from typing import Any

from rich.live import Live

from src.ai.provider_manager import AIManager
from src.cli import display as ui
from src.cli.questions import Cancelled, ask_questions, build_search_config, default_answers
from src.core.configuration import Settings, load_settings
from src.core.exceptions import CVError, ConfigurationError, JobIntelError, ScannedPDFError, StorageError
from src.core.orchestrator import Orchestrator, build_http
from src.core.pipeline import PipelineState
from src.cv.models import CandidateProfile, ParsedCV
from src.cv.parser import list_cv_files, parse_cv
from src.cv.profile_builder import ProfileBuilder
from src.storage.history import PreferenceStore
from src.utils.logger import setup_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python main.py", description="AI Job Intelligence & Discovery System")
    p.add_argument("--cv", help="CV file name inside the cv/ folder (skips the CV selection prompt)")
    p.add_argument("--defaults", action="store_true",
                   help="Non-interactive: use saved preferences/defaults for every question and start immediately")
    p.add_argument("--no-ai", action="store_true", help="Do not use the AI provider for this run")
    p.add_argument("--max-queries", type=int, help="Override budget.max_search_engine_queries for this run")
    p.add_argument("--check", action="store_true", help="Check configuration and source availability, then exit")
    p.add_argument("--history", action="store_true", help="List previous search sessions, then exit")
    p.add_argument("--report", metavar="SESSION_ID", help="Regenerate DOCX/XLSX reports for a stored session")
    return p.parse_args(argv)


def credentials_summary(settings: Settings, ai_used: bool) -> dict[str, str]:
    c = settings.credentials
    return {
        "AI provider": (f"{c.ai_provider} (key set)" + ("" if ai_used else " - not used this run"))
        if c.has_ai else "not configured - deterministic analysis",
        "Search provider": ", ".join(p for p in [c.search_provider if c.search_api_key else "",
                                                 c.secondary_search_provider if c.secondary_search_api_key else ""] if p)
        or "not configured - public job APIs and ATS boards only",
    }


class Application:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings: Settings | None = None
        self.prefs: PreferenceStore | None = None
        self.saved: dict[str, Any] = {}
        self.interactive = not args.defaults and sys.stdin.isatty() and sys.stdout.isatty()

    # ---------------------------------------------------------------- checks
    def environment(self) -> bool:
        ui.banner()
        ui.info("Checking application environment...")
        print()
        if sys.version_info < (3, 11):
            ui.error(f"Python 3.11+ required, found {platform.python_version()}.")
            return False
        ui.ok(f"Python {platform.python_version()} on {platform.system()} {platform.release()}.")
        try:
            self.settings = load_settings()
        except ConfigurationError as exc:
            ui.error(str(exc))
            return False
        if self.args.max_queries is not None:
            self.settings.raw.setdefault("budget", {})["max_search_engine_queries"] = max(0, self.args.max_queries)
        log_path = setup_logging(self.settings.log_directory, secrets=self.settings.credentials.secret_values())
        ui.ok("Configuration loaded.")
        cv_dir = self.settings.cv_directory
        cv_dir.mkdir(parents=True, exist_ok=True)
        ui.ok(f"CV directory detected: {cv_dir}")
        try:
            orch = Orchestrator(self.settings)
            orch.close()
        except StorageError as exc:
            ui.error(str(exc))
            return False
        ui.ok("Database initialized.")
        c = self.settings.credentials
        (ui.ok if c.has_ai else ui.warn)("AI provider: " + (f"{c.ai_provider} key configured" if c.has_ai else
                                                            "GEMINI_API_KEY not set - deterministic CV analysis only"))
        (ui.ok if c.has_search else ui.warn)("Search provider: " + (f"{c.search_provider} key configured" if c.has_search
                                             else "SEARCH_API_KEY not set - web/portal/social search unavailable; "
                                                  "public job APIs and ATS boards still work"))
        ui.info(f"Log file: {log_path}")
        self.prefs = PreferenceStore(self.settings.preferences_file)
        self.saved = self.prefs.load()
        return True

    # -------------------------------------------------------------------- CV
    def choose_cv(self) -> ParsedCV | None:
        assert self.settings is not None
        files = list_cv_files(self.settings.cv_directory)
        if not files:
            ui.error(f"No CV found. Put a PDF, DOCX or TXT CV into: {self.settings.cv_directory}")
            return None
        print()
        ui.console.print("[bold]Available CV files:[/]")
        for i, f in enumerate(files, 1):
            ui.console.print(f"  {i}. {f.name}")
        candidates = files
        if self.args.cv:
            match = [f for f in files if f.name.lower() == self.args.cv.lower()]
            if not match:
                ui.error(f"CV '{self.args.cv}' not found in {self.settings.cv_directory}")
                return None
            candidates = match
        elif self.interactive and len(files) > 1:
            import questionary

            last = self.saved.get("last_cv")
            default = last if last in [f.name for f in files] else files[0].name
            name = questionary.select("Select CV:", choices=[f.name for f in files], default=default).ask()
            if name is None:
                return None
            candidates = [f for f in files if f.name == name]
        else:
            last = self.saved.get("last_cv")
            candidates = [f for f in files if f.name == last] or files[:1]
        path: Path = candidates[0]
        try:
            cv = parse_cv(path)
        except ScannedPDFError as exc:
            ui.error(str(exc))
            return None
        except CVError as exc:
            ui.error(str(exc))
            return None
        ui.ok(f"CV successfully loaded: {cv.filename} ({cv.pages} page(s), {cv.char_count:,} characters).")
        for w in cv.warnings:
            ui.warn(w)
        self.saved["last_cv"] = cv.filename
        return cv

    def ai_consent(self) -> bool:
        assert self.settings is not None
        if self.args.no_ai or not self.settings.credentials.has_ai or not self.settings.get("ai.enabled", True):
            return False
        saved = self.saved.get("ai_cv_consent")
        if not self.interactive:
            return bool(saved) if saved is not None else True
        import questionary

        answer = questionary.confirm(
            "Send a redacted copy of the CV text (e-mail addresses, phone numbers and links removed) to Google Gemini "
            "for profile analysis?", default=True if saved is None else bool(saved)).ask()
        if answer is None:
            raise Cancelled()
        self.saved["ai_cv_consent"] = bool(answer)
        return bool(answer)

    async def build_profile(self, cv: ParsedCV, use_ai: bool) -> CandidateProfile:
        assert self.settings is not None
        http = build_http(self.settings, timeout=float(self.settings.get("ai.request_timeout_seconds", 90)))
        try:
            ai = AIManager.from_settings(self.settings, http) if use_ai else None
            return await ProfileBuilder(self.settings, ai).build(cv, use_ai=use_ai)
        finally:
            await http.aclose()

    # ------------------------------------------------------------- questions
    def questionnaire(self, profile: CandidateProfile) -> Any:
        assert self.settings is not None and self.prefs is not None
        saved_answers = self.saved.get("answers")
        answers = default_answers(self.settings, saved_answers, profile)
        while True:
            if self.interactive:
                ui.section("SEARCH CONFIGURATION")
                ui.console.print("[dim]Press Enter to keep the highlighted/checked default for each question.[/]")
                answers = ask_questions(self.settings, answers, profile)
            config = build_search_config(answers, self.settings, profile)
            ui.show_config(config, credentials_summary(self.settings, bool(self.saved.get("ai_cv_consent", True))))
            if not self.interactive:
                break
            import questionary

            choice = questionary.select("Ready?", choices=["Confirm and start", "Edit configuration", "Cancel"],
                                        default="Confirm and start").ask()
            if choice in (None, "Cancel"):
                raise Cancelled()
            if choice == "Confirm and start":
                break
        # Persist reusable answers (confirmation required when changing existing defaults).
        to_save = {k: v for k, v in answers.items()}
        if saved_answers is None:
            self.saved["answers"] = to_save
        elif to_save != saved_answers:
            if self.interactive:
                import questionary

                if questionary.confirm("Save these answers as your new defaults?", default=True).ask():
                    self.saved["answers"] = to_save
        self.prefs.save(self.saved)
        return config

    # ------------------------------------------------------------------ run
    async def run_pipeline(self, profile: CandidateProfile, config: Any, profile_id: int | None) -> int:
        assert self.settings is not None
        state = PipelineState()
        orch = Orchestrator(self.settings, state=state)
        try:
            with Live(ui.progress_renderable(state), console=ui.console, refresh_per_second=4, transient=False) as live:
                state.listener = lambda s: live.update(ui.progress_renderable(s))
                result = await orch.run(profile, config, profile_id=profile_id)
                live.update(ui.progress_renderable(state))
        finally:
            orch.close()
        if result.data is not None:
            ui.show_completion(result.data, result.report_files, result.status)
        for e in result.errors:
            ui.warn(e)
        return 0 if result.report_files else 1

    async def main_flow(self) -> int:
        assert self.settings is not None
        if not self.interactive:
            ui.info("Non-interactive mode: saved preferences / defaults are used for every question.")
        cv = self.choose_cv()
        if cv is None:
            return 1
        use_ai = self.ai_consent()
        ui.info("Analyzing candidate profile" + (" (deterministic + Gemini)..." if use_ai else " (deterministic)..."))
        profile = await self.build_profile(cv, use_ai)
        orch = Orchestrator(self.settings)
        try:
            profile_id = orch.repo.save_profile(profile)
        finally:
            orch.close()
        ui.show_profile(profile)
        ui.ok("Candidate profile generated.")
        config = self.questionnaire(profile)
        return await self.run_pipeline(profile, config, profile_id)

    # ---------------------------------------------------------- utilities
    async def check_sources(self) -> int:
        from rich.table import Table

        from src.core.models import SearchConfig
        from src.discovery.query_generator import QueryGenerator
        from src.discovery.source_registry import SourceRegistry
        from src.intelligence.relevance import Prefilter
        from src.sources.base import DiscoveryContext
        from src.sources.search_engine.providers import SearchService

        assert self.settings is not None
        profile = CandidateProfile(current_title="Software Engineer", target_positions=["Software Engineer"],
                                   role_families=["backend", "software_engineering"], skills=["Python"])
        config = SearchConfig()
        http = build_http(self.settings)
        try:
            search = SearchService.from_settings(self.settings, http)
            ctx = DiscoveryContext(settings=self.settings, http=http, profile=profile, config=config,
                                   plan=QueryGenerator(self.settings).generate(profile, config, budget=0),
                                   prefilter=Prefilter(self.settings, profile, config), search=search)
            table = Table(title="Source health check")
            for col in ("Source", "Category", "Status", "Detail"):
                table.add_column(col)
            for src in SourceRegistry(self.settings).sources():
                if src.uses_search_provider:
                    ok, detail = src.is_configured(ctx)
                    detail = detail or "configured (not queried to save budget)"
                else:
                    try:
                        ok, detail = await asyncio.wait_for(src.health_check(ctx), timeout=60)
                    except Exception as exc:  # noqa: BLE001
                        ok, detail = False, f"{type(exc).__name__}: {exc}"
                table.add_row(src.name, src.category.label, "[green]OK[/]" if ok else "[yellow]unavailable[/]", detail[:80])
            ai = AIManager.from_settings(self.settings, http)
            if ai.available and await ai.prepare():
                table.add_row("Gemini", "AI provider", "[green]OK[/]", f"model {ai.model}")
            else:
                table.add_row("Gemini", "AI provider", "[yellow]unavailable[/]", ai.disabled_reason)
            ui.console.print(table)
        finally:
            await http.aclose()
        return 0

    def show_history(self) -> int:
        from rich.table import Table

        assert self.settings is not None
        orch = Orchestrator(self.settings)
        try:
            sessions = orch.repo.list_sessions(30)
        finally:
            orch.close()
        import json

        from src.utils.date_time import format_local, from_iso

        table = Table(title="Search history")
        table.add_column("Session ID", no_wrap=True)
        table.add_column(f"Started ({self.settings.timezone})", no_wrap=True)
        table.add_column("Status")
        table.add_column("Unique", justify="right")
        table.add_column("Report folder", overflow="fold")
        for s in sessions:
            stats = json.loads(s.get("stats_json") or "{}")
            folder = Path(s["report_dir"]).name if s.get("report_dir") else "-"
            table.add_row(s["id"], format_local(from_iso(s["started_at"]), self.settings.timezone), s["status"],
                          str(stats.get("unique_opportunities", "-")), folder)
        ui.console.print(table)
        ui.info("Regenerate reports for a session with: python main.py --report <Session ID>")
        return 0

    def regenerate(self, session_id: str) -> int:
        assert self.settings is not None
        orch = Orchestrator(self.settings)
        try:
            folder, files = orch.regenerate_reports(session_id)
        except ValueError as exc:
            ui.error(str(exc))
            return 1
        finally:
            orch.close()
        for p in files.values():
            ui.ok(str(p))
        return 0


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = Application(args)
    try:
        if not app.environment():
            return 1
        if args.history:
            return app.show_history()
        if args.report:
            return app.regenerate(args.report)
        if args.check:
            return asyncio.run(app.check_sources())
        return asyncio.run(app.main_flow())
    except Cancelled:
        ui.warn("Cancelled by user. No search was started.")
        return 130
    except KeyboardInterrupt:
        ui.warn("Interrupted.")
        return 130
    except JobIntelError as exc:
        ui.error(str(exc))
        return 1
