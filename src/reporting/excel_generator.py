"""XLSX report generation (openpyxl): autofilters, frozen panes, hyperlinks, date formats.

All text from external sources is written as *string* cells (never formulas), which
prevents spreadsheet formula injection from scraped content.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.core.exceptions import ReportGenerationError
from src.core.models import JobOpportunity, RelevanceCategory, SourceType
from src.reporting.report_models import STATUS_LABEL, ReportData, apply_channel_label, safe_text
from src.utils.date_time import to_local

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="0563C1", underline="single")
CATEGORY_FILL = {
    RelevanceCategory.HIGHLY_RELEVANT.value: "C6EFCE", RelevanceCategory.RELEVANT.value: "DDEBF7",
    RelevanceCategory.POTENTIALLY_RELEVANT.value: "FFF2CC", RelevanceCategory.NEEDS_MANUAL_REVIEW.value: "FCE4D6",
    RelevanceCategory.NOT_RELEVANT.value: "EDEDED",
}
DATE_FMT = "yyyy-mm-dd hh:mm"


class Col:
    def __init__(self, header: str, getter: Callable[[JobOpportunity], Any], width: int = 18, link: bool = False,
                 date: bool = False) -> None:
        self.header, self.getter, self.width, self.link, self.date = header, getter, width, link, date


class ExcelReportGenerator:
    def __init__(self, data: ReportData) -> None:
        self.d = data
        self.wb = Workbook()

    # ---------------------------------------------------------------- helpers
    def _local(self, dt: datetime | None) -> datetime | None:
        loc = to_local(dt, self.d.timezone)
        return loc.replace(tzinfo=None) if loc else None  # Excel has no timezone support

    def _write(self, ws: Worksheet, row: int, col: int, value: Any, *, link: bool = False, date: bool = False) -> None:
        cell = ws.cell(row=row, column=col)
        if date:
            cell.value = value
            cell.number_format = DATE_FMT
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cell.value = value
            return
        text = safe_text(value, 32000)
        cell.value = text
        cell.data_type = "s"  # never interpret scraped text as a formula
        if link and text.startswith("http") and len(text) <= 2000:
            cell.hyperlink = text
            cell.font = LINK_FONT

    def _sheet(self, title: str, columns: list[Col], jobs: list[JobOpportunity]) -> Worksheet:
        ws = self.wb.create_sheet(title)
        for i, c in enumerate(columns, 1):
            cell = ws.cell(row=1, column=i, value=c.header)
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = c.width
        for r, job in enumerate(jobs, 2):
            for i, c in enumerate(columns, 1):
                self._write(ws, r, i, c.getter(job), link=c.link, date=c.date)
            fill = CATEGORY_FILL.get(job.relevance_category)
            if fill:
                ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor=fill)
        ws.freeze_panes = "C2" if len(columns) > 3 else "A2"
        if jobs:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(jobs) + 1}"
        ws.row_dimensions[1].height = 30
        return ws

    def _table(self, ws: Worksheet, start_row: int, header: list[str], rows: list[list[Any]],
               widths: list[int] | None = None, link_cols: set[int] | None = None, filter_: bool = True) -> int:
        for i, h in enumerate(header, 1):
            cell = ws.cell(row=start_row, column=i, value=h)
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
            if widths:
                ws.column_dimensions[get_column_letter(i)].width = max(ws.column_dimensions[get_column_letter(i)].width or 0,
                                                                       widths[i - 1])
        for r, row in enumerate(rows, start_row + 1):
            for i, v in enumerate(row, 1):
                self._write(ws, r, i, v, link=bool(link_cols and i in link_cols))
        if filter_ and rows:
            ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(header))}{start_row + len(rows)}"
        return start_row + len(rows) + 2

    # ---------------------------------------------------------------- columns
    def columns(self) -> list[Col]:
        d = self.d
        return [
            Col("Category", lambda j: j.relevance_category, 20),
            Col("Score", lambda j: int(j.relevance_score or 0), 8),
            Col("Job Title", lambda j: j.job_title, 38),
            Col("Company", lambda j: j.company_name or "", 24),
            Col("Country", lambda j: j.country or "", 14),
            Col("City", lambda j: j.city or "", 14),
            Col("Location (as stated)", lambda j: j.location or "", 24),
            Col("Work Arrangement", lambda j: j.work_arrangement, 12),
            Col("Employment Type", lambda j: j.employment_type, 12),
            Col("Seniority", lambda j: j.seniority, 10),
            Col("Salary Min", lambda j: j.salary_min if j.salary_min is not None else "", 12),
            Col("Salary Max", lambda j: j.salary_max if j.salary_max is not None else "", 12),
            Col("Currency (original)", lambda j: j.salary_currency or "", 10),
            Col("Salary Period", lambda j: j.salary_period or "", 10),
            Col("Salary (as stated)", lambda j: j.salary_text or ("Not disclosed" if j.salary_min is None else ""), 22),
            Col(f"Published ({d.timezone})", lambda j: self._local(j.published_at), 17, date=True),
            Col(f"Post Date ({d.timezone})", lambda j: self._local(j.post_created_at), 17, date=True),
            Col(f"Discovered ({d.timezone})", lambda j: self._local(j.discovered_at), 17, date=True),
            Col("Verification Status", lambda j: j.verification_status, 16),
            Col("Verification Reason", lambda j: j.verification_reason, 50),
            Col("Source Type", lambda j: SourceType(j.source_type).label if j.source_type in SourceType._value2member_map_ else j.source_type, 18),
            Col("Source Name", lambda j: j.source_name, 22),
            Col("Source URL", lambda j: j.source_url, 45, link=True),
            Col("Application URL", lambda j: j.application_url or "", 45, link=True),
            Col("Apply Channel", lambda j: apply_channel_label(j), 14),
            Col("Application Email", lambda j: ", ".join(j.application_emails), 28),
            Col("Application Status", lambda j: d.application_statuses.get(j.job_id, "Not applied"), 20),
            Col("Additional Sources", lambda j: "\n".join(f"{s.source_name}: {s.source_url}" for s in j.additional_sources), 45),
            Col("Matched Skills", lambda j: ", ".join(j.match.matched_skills), 30),
            Col("Missing Requirements", lambda j: ", ".join(j.match.missing_skills), 30),
            Col("Match Explanation", lambda j: " ".join(j.match.explanation), 60),
            Col("AI Assessment", lambda j: j.match.ai_assessment, 45),
            Col("Seniority Assessment", lambda j: j.match.seniority_assessment, 30),
            Col("Location Fit", lambda j: j.match.arrangement_assessment, 30),
            Col("To Verify", lambda j: ", ".join(j.match.needs_verification), 30),
            Col("Warnings", lambda j: " | ".join(j.warnings), 40),
            Col("New This Search", lambda j: "Yes" if j.is_new else "No", 9),
            Col("From History", lambda j: "Yes" if j.from_history else "No", 9),
            Col("Possible Duplicates (job ids)", lambda j: ", ".join(j.possible_duplicates), 20),
            Col("Job ID", lambda j: j.job_id, 18),
        ]

    # ----------------------------------------------------------------- sheets
    def summary_sheet(self) -> None:
        d = self.d
        ws = self.wb.active
        ws.title = "Executive Summary"
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 80
        ws["A1"] = "Job Search Intelligence Report"
        ws["A1"].font = Font(bold=True, size=16, color="1F3A5F")
        c = d.summary_counts()
        rows: list[tuple[str, Any]] = [
            ("Session ID", d.session_id), ("Status", d.status),
            ("Search date", d.fmt(d.started_at, False)), ("Search started", d.fmt(d.started_at)),
            ("Search completed", d.fmt(d.completed_at)), ("Timezone", d.timezone),
            ("CV file", d.profile.cv_filename), ("Primary profile", d.profile.current_title),
            ("Experience (years, from CV)", d.profile.experience_years if d.profile.experience_years is not None else "Unknown"),
            ("Estimated seniority", d.profile.seniority_estimate or "Unknown"),
            ("Proven skills", ", ".join(d.profile.skills)),
            ("Target positions", ", ".join(d.config.target_positions or d.profile.target_positions)),
            ("Countries searched", ", ".join(d.countries_searched)),
            ("Employment types", ", ".join(d.config.employment_types)),
            ("Work arrangements", ", ".join(d.config.work_arrangements)),
            ("Publication window (days)", d.config.publication_window_days or "All"),
            ("Search languages", ", ".join(d.config.languages)),
            ("Search providers", ", ".join(d.search_providers) or "None configured"),
            ("AI assistance", d.ai_status),
        ]
        rows += [(k.replace("_", " ").capitalize(), v) for k, v in c.items()]
        for i, (k, v) in enumerate(rows, 3):
            ws.cell(row=i, column=1, value=k).font = Font(bold=True)
            self._write(ws, i, 2, v)
        ws.freeze_panes = "A3"

    def coverage_sheet(self) -> None:
        d = self.d
        ws = self.wb.create_sheet("Source Coverage")
        rows = [[r.name, SourceType(r.category).label if r.category in SourceType._value2member_map_ else r.category,
                 STATUS_LABEL.get(r.status, r.status), r.queries_executed, r.items_scanned, r.results,
                 "; ".join(r.notes), "; ".join(r.errors)] for r in sorted(d.source_reports, key=lambda r: (r.category, r.name))]
        nxt = self._table(ws, 1, ["Source", "Category", "Status", "Queries/Requests", "Items Scanned", "Results Kept",
                                  "Notes", "Errors"], rows, [30, 26, 30, 12, 12, 12, 60, 60])
        ws.freeze_panes = "B2"
        ws.cell(row=nxt, column=1, value="Opportunities by country").font = Font(bold=True)
        nxt = self._table(ws, nxt + 1, ["Country", "Opportunities"], [[k, v] for k, v in d.country_counts().items()],
                          filter_=False)
        ws.cell(row=nxt, column=1, value="Coverage limitations").font = Font(bold=True)
        for i, lim in enumerate(d.limitations, nxt + 1):
            self._write(ws, i, 1, lim)

    def queries_sheet(self) -> None:
        ws = self.wb.create_sheet("Queries Executed")
        rows = [[q.source_name, q.category, q.query, q.status, q.result_count, q.error or "", q.executed_at]
                for q in self.d.queries]
        self._table(ws, 1, ["Source", "Category", "Query / Request", "Status", "Results", "Error", "Executed (UTC)"],
                    rows, [26, 16, 80, 12, 9, 40, 26])
        ws.freeze_panes = "A2"

    def tracker_sheet(self) -> None:
        ws = self.wb.create_sheet("Application Tracker")
        rows = [
            [
                a.get("id", ""),
                a.get("channel", "email"),
                a.get("mode", ""),
                a.get("status", ""),
                int(round(a["match_score"])) if a.get("match_score") is not None else "",
                a.get("company_name", ""),
                a.get("job_title", ""),
                a.get("recipient_or_url", ""),
                a.get("cv_file", ""),
                str(a.get("updated_at") or "")[:16],
                str(a.get("sent_at") or "")[:16],
                a.get("notes", ""),
            ]
            for a in self.d.applications
        ]
        self._table(
            ws,
            1,
            ["ID", "Channel", "Mode", "Status", "Score", "Company", "Job Title", "Recipient / Apply URL",
             "CV File", "Updated", "Sent / Applied", "Notes"],
            rows,
            [14, 10, 10, 18, 8, 22, 34, 42, 38, 17, 17, 45],
            link_cols={8},
        )
        ws.freeze_panes = "A2"

    def build(self) -> Workbook:
        d = self.d
        cols = self.columns()
        self.summary_sheet()
        self._sheet("All Opportunities", cols, d.sorted_jobs())
        self._sheet("Highly Relevant", cols, d.by_category(RelevanceCategory.HIGHLY_RELEVANT))
        self._sheet("Social Media Findings", cols, d.social_and_community())
        intl = [j for jobs in d.international().values() for j in jobs]
        self._sheet("International Jobs", cols, intl)
        self._sheet("Needs Verification", cols, d.needs_attention())
        if d.applications:
            self.tracker_sheet()
        self.coverage_sheet()
        self.queries_sheet()
        return self.wb

    def save(self, path: Path) -> Path:
        try:
            self.build().save(str(path))
        except Exception as exc:  # noqa: BLE001
            raise ReportGenerationError(f"XLSX generation failed: {exc}") from exc
        return path
