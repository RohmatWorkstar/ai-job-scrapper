from __future__ import annotations

import zipfile
from datetime import timedelta

from docx import Document
from openpyxl import load_workbook

from src.core.models import ExecutedQuery, JobOpportunity, SearchConfig, SourceRef, SourceRunReport, SourceType
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.reporting.docx_generator import DocxReportGenerator
from src.reporting.excel_generator import ExcelReportGenerator
from src.reporting.report_models import ReportData
from src.storage.database import Database
from src.storage.history import SearchHistory
from src.storage.repository import Repository
from src.utils.date_time import utc_now

NOW = utc_now()


def _jobs() -> list[JobOpportunity]:
    a = JobOpportunity(job_id="a1", job_title="Backend Engineer", company_name="Contoh", country="Indonesia",
                       source_type=SourceType.ATS.value, source_name="Greenhouse (contoh)",
                       source_url="https://job-boards.greenhouse.io/contoh/jobs/1",
                       application_url="https://job-boards.greenhouse.io/contoh/jobs/1#app", discovered_at=NOW,
                       published_at=NOW - timedelta(days=1), relevance_score=80, relevance_category="HIGHLY RELEVANT",
                       verification_status="VERIFIED_OPEN", verification_reason="Listed on live ATS board",
                       salary_min=18e6, salary_max=25e6, salary_currency="IDR", salary_period="month")
    a.additional_sources.append(SourceRef("job_portal", "JobStreet", "https://www.jobstreet.co.id/job/1", NOW.isoformat()))
    b = JobOpportunity(job_id="b2", job_title="=HYPERLINK(\"http://evil\",\"x\")", company_name="@risky",
                       country="Germany", source_type=SourceType.SOCIAL_MEDIA.value, source_name="LinkedIn Posts",
                       source_url="https://www.linkedin.com/posts/abc", discovered_at=NOW, relevance_score=45,
                       relevance_category="POTENTIALLY RELEVANT", verification_status="NEEDS_REVIEW",
                       verification_reason="Third-party post", content_kind="social_post",
                       job_description="bad\x00control\x07chars")
    return [a, b]


def _report(settings, sample_cv_path) -> ReportData:
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    return ReportData(session_id="s1", started_at=NOW, completed_at=NOW, timezone="Asia/Jakarta", status="completed",
                      profile=profile, config=SearchConfig(), jobs=_jobs(), total_discoveries=3, duplicates_merged=1,
                      source_reports=[SourceRunReport("Greenhouse", "ats_platform", "success", 2, 10, 1),
                                      SourceRunReport("Web Search", "search_engine", "skipped_no_credentials",
                                                      notes=["No search API configured"])],
                      queries=[ExecutedQuery("Greenhouse", "ats_platform", "board:contoh", "ok", 1, NOW.isoformat())],
                      ai_status="disabled", limitations=["Test limitation"], countries_searched=["Indonesia"])


def test_docx_report_with_clickable_links(settings, sample_cv_path, tmp_path) -> None:
    path = DocxReportGenerator(_report(settings, sample_cv_path)).save(tmp_path / "r.docx")
    doc = Document(str(path))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading 1")]
    for n in range(1, 10):
        assert any(h.startswith(f"{n}.") for h in headings), n
    with zipfile.ZipFile(path) as z:
        rels = z.read("word/_rels/document.xml.rels").decode()
        body = z.read("word/document.xml").decode()
    assert "https://job-boards.greenhouse.io/contoh/jobs/1" in rels
    assert 'TargetMode="External"' in rels
    assert "<w:hyperlink" in body
    assert "Not searched - credentials missing" in body


def test_xlsx_report(settings, sample_cv_path, tmp_path) -> None:
    path = ExcelReportGenerator(_report(settings, sample_cv_path)).save(tmp_path / "r.xlsx")
    wb = load_workbook(path)
    for name in ("Executive Summary", "All Opportunities", "Highly Relevant", "Social Media Findings",
                 "International Jobs", "Needs Verification", "Source Coverage"):
        assert name in wb.sheetnames
    ws = wb["All Opportunities"]
    headers = [c.value for c in ws[1]]
    url_col = headers.index("Source URL") + 1
    assert ws.cell(row=2, column=url_col).hyperlink.target.startswith("https://")
    assert ws.freeze_panes == "C2" and ws.auto_filter.ref
    title_col = headers.index("Job Title") + 1
    titles = [ws.cell(row=r, column=title_col) for r in (2, 3)]
    injected = [c for c in titles if str(c.value).startswith("=")][0]
    assert injected.data_type == "s"  # stored as text, not a formula
    pub_col = headers.index("Published (Asia/Jakarta)") + 1
    assert ws.cell(row=2, column=pub_col).number_format == "yyyy-mm-dd hh:mm"
    assert len(list(wb["Social Media Findings"].iter_rows(min_row=2))) == 1
    assert len(list(wb["International Jobs"].iter_rows(min_row=2))) == 1


def test_repository_and_history(settings, sample_cv_path) -> None:
    db = Database(settings.database_path)
    repo = Repository(db)
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    pid = repo.save_profile(profile)
    assert repo.profile_by_id(pid).current_title == "Fullstack Developer"
    assert repo.latest_profile_for(profile.cv_sha256) is not None
    history = SearchHistory(repo)
    repo.create_session("s1", pid, SearchConfig())
    jobs = _jobs()
    assert history.mark_new(jobs) == 2
    repo.save_opportunities("s1", jobs)
    repo.finish_session("s1", "completed", {"unique_opportunities": 2}, None)
    repo.create_session("s2", pid, SearchConfig())
    again = _jobs()[:1]
    assert history.mark_new(again) == 0 and again[0].is_new is False and again[0].first_seen_at is not None
    repo.save_opportunities("s2", again)
    prev = history.previous_jobs("s2")
    assert [j.job_id for j in prev] == ["b2"] and prev[0].from_history  # old jobs are not suppressed
    stored = repo.session_opportunities("s1")
    assert {j.job_id for j in stored} == {"a1", "b2"}
    assert stored[0].additional_sources or stored[1].additional_sources
    assert len(repo.verification_history("a1")) == 2
    db.close()
