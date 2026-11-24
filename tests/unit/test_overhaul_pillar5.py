"""Tests for Pillar 5 (XLSX & DOCX application channel, email, status, and Application Tracker sheet)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from src.applications.models import ApplicationStatus
from src.applications.tracker import ApplicationTracker
from src.core.models import JobOpportunity, SearchConfig, SourceType
from src.core.orchestrator import Orchestrator
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.reporting.docx_generator import DocxReportGenerator
from src.reporting.excel_generator import ExcelReportGenerator
from src.reporting.report_models import ReportData, apply_channel_label
from src.utils.date_time import utc_now

NOW = utc_now()


def _sample_jobs() -> list[JobOpportunity]:
    return [
        JobOpportunity(
            job_id="j_both",
            job_title="Backend Engineer",
            company_name="Acme Pay",
            country="Indonesia",
            source_type=SourceType.ATS.value,
            source_name="Greenhouse (acme)",
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            application_url="https://boards.greenhouse.io/acme/jobs/1",
            application_emails=["jobs@acmepay.id"],
            discovered_at=NOW,
            relevance_score=85.0,
            relevance_category="HIGHLY RELEVANT",
            verification_status="VERIFIED_OPEN",
        ),
        JobOpportunity(
            job_id="j_web",
            job_title="Fullstack Engineer",
            company_name="Flip",
            country="Indonesia",
            source_type=SourceType.ATS.value,
            source_name="Ashby (flip)",
            source_url="https://jobs.ashbyhq.com/flip/2",
            application_url="https://jobs.ashbyhq.com/flip/2",
            discovered_at=NOW,
            relevance_score=78.0,
            relevance_category="HIGHLY RELEVANT",
            verification_status="VERIFIED_OPEN",
        ),
    ]


def test_apply_channel_labels_and_empty_tracker_omits_sheet(settings, sample_cv_path, tmp_path: Path) -> None:
    jobs = _sample_jobs()
    assert apply_channel_label(jobs[0]) == "Email & Web"
    assert apply_channel_label(jobs[1]) == "Web/ATS"

    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    data = ReportData(
        session_id="s1", started_at=NOW, completed_at=NOW, timezone="Asia/Jakarta", status="completed",
        profile=profile, config=SearchConfig(), jobs=jobs, total_discoveries=2, duplicates_merged=0,
        source_reports=[], queries=[],
    )
    assert data.summary_counts()["email_applicable"] == 1

    xlsx = ExcelReportGenerator(data).save(tmp_path / "empty.xlsx")
    wb = load_workbook(xlsx)
    assert "Application Tracker" not in wb.sheetnames
    headers = [c.value for c in wb["All Opportunities"][1]]
    assert "Apply Channel" in headers
    assert "Application Email" in headers
    assert "Application Status" in headers


def test_reports_include_tracker_sheet_emails_and_statuses_via_orchestrator(
    settings, sample_cv_path, tmp_path: Path
) -> None:
    settings.raw["reporting"]["output_directory"] = str(tmp_path / "reports")
    orch = Orchestrator(settings)
    try:
        profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
        pid = orch.repo.save_profile(profile)
        orch.repo.create_session("sess1", pid, SearchConfig())
        jobs = _sample_jobs()
        orch.repo.save_opportunities("sess1", jobs)
        orch.repo.finish_session("sess1", "completed", {"unique_opportunities": 2}, None)

        tr = ApplicationTracker(orch.db)
        web_rec = tr.create(
            mode="ai", channel="web", job_key="j_web", company_name="Flip", job_title="Fullstack Engineer",
            job_url="https://jobs.ashbyhq.com/flip/2", status="PENDING_APPROVAL", match_score=82.0,
        )
        tr.mark_applied_web(web_rec.id, note="Submitted on Ashby")
        tr.update_pipeline_status(web_rec.id, ApplicationStatus.INTERVIEW, note="First round")

        _, files = orch.regenerate_reports("sess1")
        wb = load_workbook(files["xlsx"])
        assert "Application Tracker" in wb.sheetnames
        tracker_ws = wb["Application Tracker"]
        assert tracker_ws.max_row == 2
        row_vals = [tracker_ws.cell(row=2, column=c).value for c in range(1, 13)]
        assert row_vals[1] == "web" and row_vals[3] == "INTERVIEW" and row_vals[5] == "Flip"

        all_ws = wb["All Opportunities"]
        headers = [c.value for c in all_ws[1]]
        status_col = headers.index("Application Status") + 1
        email_col = headers.index("Application Email") + 1
        statuses = {all_ws.cell(row=r, column=headers.index("Job ID") + 1).value: all_ws.cell(row=r, column=status_col).value
                    for r in (2, 3)}
        emails = {all_ws.cell(row=r, column=headers.index("Job ID") + 1).value: all_ws.cell(row=r, column=email_col).value
                  for r in (2, 3)}
        assert statuses["j_web"] == "INTERVIEW (web)"
        assert statuses["j_both"] == "Not applied"
        assert emails["j_both"] == "jobs@acmepay.id"

        # DOCX check: executive summary + mailto hyperlink + Application status
        doc = Document(str(files["docx"]))
        all_table_text = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
        assert "Email-applicable opportunities" in all_table_text
        assert "INTERVIEW (web)" in all_table_text
        with zipfile.ZipFile(files["docx"]) as z:
            rels = z.read("word/_rels/document.xml.rels").decode()
        assert "mailto:jobs@acmepay.id" in rels
    finally:
        orch.close()
