"""Rich terminal presentation."""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from src.cli.questions import ARRANGEMENTS, EMPLOYMENT, GEOGRAPHY, SOURCES, WINDOWS, _arrangement_key
from src.core.models import SearchConfig
from src.core.pipeline import STAGES, PipelineState
from src.cv.models import CandidateProfile
from src.reporting.report_models import ReportData

console = Console(highlight=False)

STATUS_STYLE = {"WAITING": "dim", "RUNNING": "bold yellow", "COMPLETED": "bold green", "FAILED": "bold red",
                "SKIPPED": "dim", "INTERRUPTED": "bold magenta"}
CATEGORY_LABELS = {"search_engine": "Search engine queries", "job_portal": "Job portal queries",
                   "company_careers": "Official career pages", "ats_platform": "ATS board requests",
                   "social_media": "Social discovery queries", "community": "Community queries", "other": "Other"}


def banner(title: str = "AI JOB INTELLIGENCE & DISCOVERY SYSTEM") -> None:
    console.print(Rule(style="cyan"))
    console.print(Text(title, style="bold cyan"), justify="center")
    console.print(Rule(style="cyan"))


def section(title: str) -> None:
    console.print()
    console.print(Rule(Text(title, style="bold"), style="cyan"))


def ok(msg: str) -> None:
    console.print(f"[bold green][OK][/] {msg}")


def info(msg: str) -> None:
    console.print(f"[bold cyan][INFO][/] {msg}")


def warn(msg: str) -> None:
    console.print(f"[bold yellow][WARN][/] {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red][ERROR][/] {msg}")


def show_profile(p: CandidateProfile) -> None:
    section("CANDIDATE PROFILE")
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Primary profile", p.current_title or "[dim]not stated in CV[/]")
    t.add_row("Experience", f"{p.experience_years} years" if p.experience_years is not None else "[dim]not determinable[/]")
    t.add_row("Estimated seniority", p.seniority_estimate or "[dim]unknown[/]")
    t.add_row("Experience areas", "\n".join(f"- {a}" for a in p.experience_areas[:6]) or "-")
    t.add_row("Languages (programming)", ", ".join(p.programming_languages) or "-")
    t.add_row("Frameworks", ", ".join(p.frameworks) or "-")
    t.add_row("Databases", ", ".join(p.databases) or "-")
    t.add_row("Tools", ", ".join(p.tools) or "-")
    if p.industry_experience:
        t.add_row("Industry experience", ", ".join(p.industry_experience))
    if p.certifications:
        t.add_row("Certifications", "\n".join(p.certifications[:5]))
    if p.languages:
        t.add_row("Spoken languages", ", ".join(p.languages))
    t.add_row("Suggested positions", ", ".join(p.target_positions[:8]) or "-")
    if p.adjacent_positions:
        t.add_row("Adjacent positions", ", ".join(p.adjacent_positions[:6]))
    if p.transferable_skills:
        t.add_row("Transferable (NOT in CV)", "[dim]" + ", ".join(p.transferable_skills[:8]) + "[/]")
    t.add_row("Analysis method", p.analysis_method)
    console.print(t)
    for n in p.notes:
        console.print(f"  [dim]• {n}[/]")


def show_config(c: SearchConfig, credentials_summary: dict[str, str]) -> None:
    section("SEARCH CONFIGURATION SUMMARY")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    geo = GEOGRAPHY.get(c.geography_mode, c.geography_mode)
    if c.geography_mode == "selected":
        geo += f": {', '.join(c.countries)}"
    t.add_row("Geography", geo)
    t.add_row("Employment type", ", ".join(EMPLOYMENT.get(e, e) for e in c.employment_types))
    t.add_row("Target positions", ", ".join(c.target_positions) or "-")
    t.add_row("Work arrangement", ARRANGEMENTS.get(_arrangement_key(c.work_arrangements), ", ".join(c.work_arrangements)))
    t.add_row("Publication date", WINDOWS.get(str(c.publication_window_days or "all"), str(c.publication_window_days)))
    sal = "No minimum" if not c.min_salary else f"{c.salary_currency or ''} {c.min_salary:,.0f}"
    t.add_row("Salary", f"{sal}; undisclosed salaries {'included' if c.include_undisclosed_salary else 'deprioritised'}")
    t.add_row("Seniority", (", ".join(c.seniority_levels) or "all relevant levels") + f"  ({c.seniority_mode})")
    t.add_row("Sources", ", ".join(SOURCES.get(s, s) for s in c.source_categories))
    t.add_row("Languages", ", ".join(c.languages))
    t.add_row("Additional preferences", c.additional_preferences or "-")
    t.add_row("Include previous jobs", "yes" if c.include_previous_jobs else "no")
    for k, v in credentials_summary.items():
        t.add_row(k, v)
    console.print(t)


def progress_renderable(state: PipelineState) -> Any:
    stages = Table(show_header=False, box=None, padding=(0, 1))
    stages.add_column(width=6)
    stages.add_column(width=32)
    stages.add_column(width=13)
    stages.add_column(style="dim")
    for i, s in enumerate(STAGES, 1):
        status = state.stages.get(s, "WAITING")
        stages.add_row(f"[{i}/{len(STAGES)}]", s, Text(status, style=STATUS_STYLE.get(status, "")), state.details.get(s, "")[:70])
    act = Table(show_header=False, box=None, padding=(0, 1))
    act.add_column(width=30)
    act.add_column(justify="right", width=8)
    for cat, label in CATEGORY_LABELS.items():
        n = state.queries_by_category.get(cat, 0)
        if n:
            act.add_row(label + ":", str(n))
    act.add_row("Results discovered:", str(state.results_discovered))
    if state.unique_opportunities is not None:
        act.add_row("Unique opportunities:", str(state.unique_opportunities))
    if state.pages_to_fetch:
        act.add_row("Pages checked:", f"{state.pages_fetched}/{state.pages_to_fetch}")
    if state.ai_calls:
        act.add_row("AI calls:", str(state.ai_calls))
    running = ", ".join(sorted(state.sources_running)[:4])
    parts: list[Any] = [Rule(Text("JOB DISCOVERY RUNNING", style="bold"), style="cyan"), stages,
                        Rule(Text("SEARCH ACTIVITY", style="bold"), style="dim"), act]
    if running:
        parts.append(Text(f"Active sources: {running}", style="yellow"))
    parts.append(Text("Press Ctrl+C once to stop early and keep partial results.", style="dim"))
    return Group(*parts)


def show_completion(data: ReportData, files: dict[str, Any], status: str) -> None:
    c = data.summary_counts()
    console.print()
    title = "SEARCH COMPLETED" if status == "completed" else f"SEARCH FINISHED ({status.upper()})"
    console.print(Rule(Text(title, style="bold green" if status == "completed" else "bold yellow"), style="green"))
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold", width=30)
    t.add_column(justify="right")
    for label, key in (("Total discoveries", "total_discoveries"), ("Unique opportunities", "unique_opportunities"),
                       ("Duplicates merged", "duplicates_merged"), ("Highly relevant", "highly_relevant"),
                       ("Relevant", "relevant"), ("Potential matches", "potentially_relevant"),
                       ("Needs manual review", "needs_manual_review"), ("Not relevant (kept in XLSX)", "not_relevant"),
                       ("Verified open", "verified_open"), ("Likely open", "likely_open"),
                       ("Unverified / needs review", None), ("Closed / inaccessible", None),
                       ("Possible scam", "possible_scam"), ("New since previous searches", "new_since_previous_searches")):
        if key:
            val = c[key]
        elif label.startswith("Unverified"):
            val = c["unverified"] + c["needs_review"]
        else:
            val = c["closed"] + c["inaccessible"]
        t.add_row(label + ":", str(val))
    console.print(t)
    console.print()
    console.print("[bold]Reports generated:[/]")
    for kind, path in files.items():
        console.print(f"  [bold green][OK][/] {path}")
    console.print()
    console.print(f"Research completed at: [bold]{data.fmt(data.completed_at)}[/]")
    console.print(f"Timezone: [bold]{data.timezone}[/]")
    failed = [r for r in data.source_reports if r.status in ("failed", "partial")]
    skipped = [r for r in data.source_reports if r.status == "skipped_no_credentials"]
    if failed:
        warn("Sources with errors: " + ", ".join(f"{r.name}" for r in failed) + " (details in report Section 9)")
    if skipped:
        info("Not searched (credentials missing): " + ", ".join(r.name for r in skipped))
    console.print(Rule(style="green"))


def show_panel(text: str, title: str = "", style: str = "cyan") -> None:
    console.print(Panel(text, title=title, border_style=style))
