"""DOCX report generation (python-docx) with clickable hyperlinks."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from src.core.exceptions import ReportGenerationError
from src.core.models import JobOpportunity, RelevanceCategory, SourceType
from src.intelligence.evidence import evidence_lines
from src.reporting.report_models import STATUS_LABEL, ReportData, safe_text, salary_display

ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
MUTED = RGBColor(0x59, 0x59, 0x59)
VERIF_COLOURS = {
    "VERIFIED_OPEN": RGBColor(0x1E, 0x7B, 0x34), "LIKELY_OPEN": RGBColor(0x2E, 0x7D, 0x9A),
    "UNVERIFIED": RGBColor(0x80, 0x80, 0x80), "NEEDS_REVIEW": RGBColor(0xB3, 0x6B, 0x00),
    "CLOSED": RGBColor(0x8B, 0x00, 0x00), "INACCESSIBLE": RGBColor(0x8B, 0x00, 0x00),
    "POSSIBLE_SCAM": RGBColor(0xC0, 0x00, 0x00),
}


def _safe_url(url: str) -> str:
    return quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~-._")


def add_hyperlink(paragraph, url: str, text: str | None = None) -> None:  # type: ignore[no-untyped-def]
    if not url:
        return
    r_id = paragraph.part.relate_to(_safe_url(url), RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(color)
    rpr.append(underline)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = safe_text(text or url, 180)
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _shade(cell, hex_fill: str) -> None:  # type: ignore[no-untyped-def]
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


class DocxReportGenerator:
    def __init__(self, data: ReportData) -> None:
        self.d = data
        self.doc = Document()
        self._setup()

    # ------------------------------------------------------------------ setup
    def _setup(self) -> None:
        for section in self.doc.sections:
            section.left_margin = section.right_margin = Cm(2)
            section.top_margin = section.bottom_margin = Cm(1.8)
        normal = self.doc.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(10)
        for name, size in (("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11)):
            st = self.doc.styles[name]
            st.font.color.rgb = ACCENT
            st.font.size = Pt(size)

    def _p(self, text: str = "", *, bold: bool = False, italic: bool = False, color: RGBColor | None = None,
           size: int | None = None):  # type: ignore[no-untyped-def]
        p = self.doc.add_paragraph()
        run = p.add_run(safe_text(text))
        run.bold, run.italic = bold, italic
        if color:
            run.font.color.rgb = color
        if size:
            run.font.size = Pt(size)
        return p

    def _bullet(self, text: str) -> None:
        self.doc.add_paragraph(safe_text(text, 600), style="List Bullet")

    def _kv_table(self, rows: list[tuple[str, str]], widths: tuple[float, float] = (5.0, 12.0)):  # type: ignore[no-untyped-def]
        table = self.doc.add_table(rows=0, cols=2)
        table.style = "Table Grid"
        for k, v in rows:
            cells = table.add_row().cells
            cells[0].text = safe_text(k)
            cells[0].paragraphs[0].runs[0].bold = True
            _shade(cells[0], "EEF2F7")
            cells[1].text = safe_text(v, 1500)
        for row in table.rows:
            row.cells[0].width, row.cells[1].width = Cm(widths[0]), Cm(widths[1])
        return table

    def _grid(self, header: list[str], rows: list[list[str]], link_cols: dict[int, int] | None = None):  # type: ignore[no-untyped-def]
        """Simple table. ``link_cols`` maps a display column to the index of the URL in the row."""
        table = self.doc.add_table(rows=1, cols=len(header))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, h in enumerate(header):
            c = table.rows[0].cells[i]
            c.text = h
            c.paragraphs[0].runs[0].bold = True
            _shade(c, "1F3A5F")
            c.paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for row in rows:
            cells = table.add_row().cells
            for i in range(len(header)):
                if link_cols and i in link_cols:
                    url = row[link_cols[i]]
                    if url:
                        add_hyperlink(cells[i].paragraphs[0], url, row[i] or "Open")
                    else:
                        cells[i].text = safe_text(row[i])
                else:
                    cells[i].text = safe_text(row[i], 400)
        for row in table.rows:
            for c in row.cells:
                for p in c.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(8.5)
        return table

    # --------------------------------------------------------------- sections
    def _title(self) -> None:
        d = self.d
        t = self.doc.add_paragraph()
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = t.add_run("Job Search Intelligence Report")
        r.bold, r.font.size, r.font.color.rgb = True, Pt(22), ACCENT
        sub = self.doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rs = sub.add_run(f"AI Job Intelligence & Discovery System  •  Session {d.session_id}")
        rs.font.color.rgb, rs.font.size = MUTED, Pt(10)
        if d.status != "completed":
            self._p(f"Note: this search ended with status '{d.status}'. The report contains the results collected "
                    "before it stopped.", bold=True, color=RGBColor(0xB3, 0x6B, 0x00))

    def _section1(self) -> None:
        d, c = self.d, self.d.summary_counts()
        self.doc.add_heading("1. Executive Summary", level=1)
        self._kv_table([
            ("Search date", d.fmt(d.started_at, with_time=False)),
            ("Search started", d.fmt(d.started_at)),
            ("Search completed", d.fmt(d.completed_at)),
            ("Timezone", d.timezone),
            ("Candidate CV", d.profile.cv_filename),
            ("Primary profile", d.profile.current_title or "Not stated in CV"),
            ("Experience (from CV)", f"{d.profile.experience_years} years" if d.profile.experience_years is not None
             else "Not determinable from CV"),
            ("Estimated seniority", d.profile.seniority_estimate or "Unknown"),
            ("Key skills (proven in CV)", ", ".join(d.profile.skills[:20]) or "None detected"),
            ("Target positions", ", ".join(d.config.target_positions or d.profile.target_positions)),
            ("Countries searched", ", ".join(d.countries_searched)),
            ("Work arrangements", ", ".join(d.config.work_arrangements)),
            ("Employment types", ", ".join(d.config.employment_types)),
            ("Publication window", f"Last {d.config.publication_window_days} days" if d.config.publication_window_days
             else "All available dates"),
            ("Total results discovered", str(c["total_discoveries"])),
            ("Unique opportunities", str(c["unique_opportunities"])),
            ("Duplicate entries merged", str(c["duplicates_merged"])),
            ("Highly relevant", str(c["highly_relevant"])),
            ("Relevant", str(c["relevant"])),
            ("Potentially relevant", str(c["potentially_relevant"])),
            ("Needs manual review", str(c["needs_manual_review"])),
            ("Unverified opportunities", str(c["unverified"])),
            ("Email-applicable opportunities", str(c["email_applicable"])),
            ("New since previous searches", str(c["new_since_previous_searches"])),
            ("AI assistance", d.ai_status),
        ])
        self._p("")
        self._p("Verification summary", bold=True)
        ver = d.verification_counts()
        self._grid(["Status", "Count", "Meaning"], [
            ["VERIFIED_OPEN", str(ver["VERIFIED_OPEN"]), "Present on the company's own recruiting channel when checked"],
            ["LIKELY_OPEN", str(ver["LIKELY_OPEN"]), "Live third-party listing or structured posting without full confirmation"],
            ["UNVERIFIED", str(ver["UNVERIFIED"]), "Known from search results only; page not confirmed"],
            ["NEEDS_REVIEW", str(ver["NEEDS_REVIEW"]), "Social/community post or unclear content - check manually"],
            ["CLOSED", str(ver["CLOSED"]), "Page states it is closed or validity date passed"],
            ["INACCESSIBLE", str(ver["INACCESSIBLE"]), "Original page could not be opened"],
            ["POSSIBLE_SCAM", str(ver["POSSIBLE_SCAM"]), "Contains common fraud warning signs"],
        ])
        self._p("Scores are estimated relevance indicators based on the CV and preferences, not probabilities of "
                "being hired.", italic=True, color=MUTED, size=8)

    def _section2(self) -> None:
        d = self.d
        self.doc.add_heading("2. Search Coverage", level=1)
        self.doc.add_heading("Opportunities by country", level=2)
        self._grid(["Country", "Opportunities"], [[k, str(v)] for k, v in d.country_counts().items()])
        self.doc.add_heading("Opportunities by source category", level=2)
        self._grid(["Source category", "Listings (incl. merged duplicates)"],
                   [[k, str(v)] for k, v in d.source_type_counts().items()])
        self.doc.add_heading("Opportunities by platform", level=2)
        self._grid(["Platform", "Listings"], [[k, str(v)] for k, v in list(d.platform_counts().items())[:40]])
        self.doc.add_heading("Search status per source", level=2)
        self._p("'Searched' means the source was actually queried in this run. Sources marked 'Not searched' are "
                "supported by the application but were not used (not selected, disabled or missing credentials).",
                italic=True, color=MUTED, size=8)
        rows = []
        for r in sorted(d.source_reports, key=lambda r: (r.category, r.name)):
            rows.append([r.name, SourceType(r.category).label if r.category in SourceType._value2member_map_ else r.category,
                         STATUS_LABEL.get(r.status, r.status), str(r.queries_executed), str(r.items_scanned or ""),
                         str(r.results), "; ".join(r.notes + r.errors)[:300]])
        self._grid(["Source", "Category", "Status", "Queries", "Scanned", "Results", "Notes"], rows)

    def _job_block(self, idx: int, j: JobOpportunity) -> None:
        d = self.d
        head = f"{idx}. {j.job_title}" + (f" — {j.company_name}" if j.company_name else "")
        self.doc.add_heading(safe_text(head, 200), level=3)
        loc = ", ".join(x for x in [j.location, j.country if j.country and j.country not in (j.location or "") else None] if x)
        rows = [
            ("Company", j.company_name or "Not identified"),
            ("Country / Location", loc or "Not stated"),
            ("Employment type", j.employment_type.replace("_", "-")),
            ("Work arrangement", j.work_arrangement),
            ("Salary", salary_display(j)),
            ("Publication date", d.fmt(j.published_at, False) or "Not stated"),
        ]
        if j.post_created_at:
            rows.append(("Post date (social/community)", d.fmt(j.post_created_at, False)))
        rows += [
            ("Discovered", d.fmt(j.discovered_at) + ("  (new)" if j.is_new else "  (seen in earlier searches)")),
            ("Match score", f"{int(j.relevance_score or 0)}/100 - {j.relevance_category}"),
            ("Why it matches", " ".join(j.match.explanation[:4])),
        ]
        if j.match.ai_assessment:
            rows.append(("AI assessment", j.match.ai_assessment))
        rows += [
            ("Skills compatibility", ", ".join(j.match.matched_skills) or "No overlapping skills detected in the text"),
            ("Missing requirements", ", ".join(j.match.missing_skills) or "None detected"),
            ("Seniority", f"{j.seniority} - {j.match.seniority_assessment}"),
            ("Location fit", j.match.arrangement_assessment),
            ("Verification", f"{j.verification_status}: {j.verification_reason}"),
        ]
        if j.match.needs_verification:
            rows.append(("To verify", ", ".join(j.match.needs_verification)))
        if j.warnings:
            rows.append(("Warnings", " | ".join(j.warnings)))
        app_status = d.application_statuses.get(j.job_id)
        if app_status:
            rows.append(("Application status", app_status))
        table = self._kv_table(rows)
        for label, url, text in (("Original source", j.source_url, f"{j.source_name}: {j.source_url}"),
                                 ("Application link", j.application_url or "", j.application_url or "")):
            cells = table.add_row().cells
            cells[0].text = label
            cells[0].paragraphs[0].runs[0].bold = True
            _shade(cells[0], "EEF2F7")
            if url:
                add_hyperlink(cells[1].paragraphs[0], url, text)
            else:
                cells[1].text = "Not identified - use the original source"
        if j.application_emails:
            cells = table.add_row().cells
            cells[0].text = "Application e-mail"
            cells[0].paragraphs[0].runs[0].bold = True
            _shade(cells[0], "EEF2F7")
            add_hyperlink(cells[1].paragraphs[0], f"mailto:{j.application_emails[0]}", j.application_emails[0])
            if len(j.application_emails) > 1:
                cells[1].paragraphs[0].add_run("  (" + ", ".join(j.application_emails[1:4]) + ")")
        if j.additional_sources:
            cells = table.add_row().cells
            cells[0].text = f"Also found at ({len(j.additional_sources)})"
            cells[0].paragraphs[0].runs[0].bold = True
            _shade(cells[0], "EEF2F7")
            first = True
            for s in j.additional_sources[:8]:
                p = cells[1].paragraphs[0] if first else cells[1].add_paragraph()
                first = False
                add_hyperlink(p, s.source_url, f"{s.source_name}: {s.source_url}")
        ev = evidence_lines(j, 5)
        if ev:
            cells = table.add_row().cells
            cells[0].text = "Evidence"
            cells[0].paragraphs[0].runs[0].bold = True
            _shade(cells[0], "EEF2F7")
            cells[1].text = safe_text("\n".join(ev), 1200)
            for r in cells[1].paragraphs[0].runs:
                r.font.size = Pt(8)
                r.font.color.rgb = MUTED
        status_colour = VERIF_COLOURS.get(j.verification_status)
        if status_colour:
            for row in table.rows:
                if row.cells[0].text == "Verification":
                    for r in row.cells[1].paragraphs[0].runs:
                        r.font.color.rgb = status_colour
        self._p("")

    def _job_section(self, number: int, title: str, jobs: list[JobOpportunity], intro: str) -> None:
        self.doc.add_heading(f"{number}. {title} ({len(jobs)})", level=1)
        self._p(intro, italic=True, color=MUTED, size=9)
        if not jobs:
            self._p("No opportunities in this category.")
            return
        for i, j in enumerate(jobs, 1):
            self._job_block(i, j)

    def _compact(self, jobs: list[JobOpportunity], extra_col: str = "Category") -> None:
        rows = []
        for j in jobs:
            extra = j.relevance_category if extra_col == "Category" else j.verification_reason
            rows.append([j.job_title, j.company_name or "-", j.country or "-",
                         self.d.fmt(j.published_at or j.post_created_at, False) or "-", str(int(j.relevance_score or 0)),
                         j.verification_status, extra, "Open", j.source_url])
        self._grid(["Title", "Company", "Country", "Date", "Score", "Verification", extra_col, "Source"],
                   rows, link_cols={7: 8})

    def _section6(self) -> None:
        jobs = self.d.social_and_community()
        self.doc.add_heading(f"6. Social Media & Community Opportunities ({len(jobs)})", level=1)
        self._p("Recruiter, HR, hiring-manager, company and community posts. Post dates are the dates of the post, "
                "not necessarily the job's original publication date. Authorization by the company is not confirmed.",
                italic=True, color=MUTED, size=9)
        if jobs:
            self._compact(jobs)
        else:
            self._p("No social media or community opportunities were found in this run (see Section 9 for which "
                    "sources were searched).")

    def _section7(self) -> None:
        groups = self.d.international()
        total = sum(len(v) for v in groups.values())
        self.doc.add_heading(f"7. International Opportunities ({total})", level=1)
        self._p("Grouped by country. Salaries are shown exactly as reported in their original currency; no currency "
                "conversion is applied.", italic=True, color=MUTED, size=9)
        if not groups:
            self._p("No international opportunities in this run.")
        for country, jobs in groups.items():
            self.doc.add_heading(f"{country} ({len(jobs)})", level=2)
            rows = [[j.job_title, j.company_name or "-", j.work_arrangement, salary_display(j),
                     str(int(j.relevance_score or 0)), j.verification_status, "Open", j.source_url] for j in jobs]
            self._grid(["Title", "Company", "Arrangement", "Salary (original)", "Score", "Verification", "Source"],
                       rows, link_cols={6: 7})

    def _section8(self) -> None:
        jobs = self.d.needs_attention()
        self.doc.add_heading(f"8. Needs Verification / Closed / Suspicious ({len(jobs)})", level=1)
        self._p("Questionable, inactive or unclear items, with the reason for each status.", italic=True, color=MUTED,
                size=9)
        if jobs:
            self._compact(jobs, extra_col="Reason")
        else:
            self._p("None.")

    def _section9(self) -> None:
        d = self.d
        self.doc.add_heading("9. Source & Research Logs", level=1)
        by = d.sources_by_status()
        ok = by.get("success", []) + by.get("partial", []) + by.get("no_results", [])
        self.doc.add_heading("Sources accessed successfully", level=2)
        for r in ok:
            self._bullet(f"{r.name}: {r.queries_executed} request(s), {r.results} result(s) kept"
                         + (f" - {r.notes[-1]}" if r.notes else ""))
        if not ok:
            self._p("None.")
        self.doc.add_heading("Sources unavailable or not used", level=2)
        unavailable = [r for s, rs in by.items() if s not in {"success", "partial", "no_results"} for r in rs]
        for r in unavailable:
            self._bullet(f"{r.name}: {STATUS_LABEL.get(r.status, r.status)}" + (f" - {'; '.join(r.notes)}" if r.notes else ""))
        if not unavailable:
            self._p("None.")
        self.doc.add_heading("Search errors", level=2)
        errs = [f"{r.name}: {e}" for r in d.source_reports for e in r.errors] + d.errors
        for e in errs[:120]:
            self._bullet(e)
        if not errs:
            self._p("No errors were recorded.")
        self.doc.add_heading("Coverage limitations", level=2)
        for lim in d.limitations:
            self._bullet(lim)
        self.doc.add_heading(f"Queries executed ({len(d.queries)})", level=2)
        self._grid(["Source", "Query / request", "Status", "Results"],
                   [[q.source_name, q.query, q.status + (f" ({q.error})" if q.error else ""), str(q.result_count)]
                    for q in d.queries])

    def _appendix(self) -> None:
        jobs = self.d.by_category(RelevanceCategory.NOT_RELEVANT)
        self.doc.add_heading(f"Appendix A. Other Results Classified as Not Relevant ({len(jobs)})", level=1)
        self._p("Kept for transparency; nothing is silently discarded. The spreadsheet contains full details.",
                italic=True, color=MUTED, size=9)
        if jobs:
            self._compact(jobs[:400])

    # ----------------------------------------------------------------- public
    def build(self) -> Document:
        d = self.d
        self._title()
        self._section1()
        self._section2()
        self._job_section(3, "Highly Relevant Opportunities", d.by_category(RelevanceCategory.HIGHLY_RELEVANT),
                          "Strongest matches with sufficient information.")
        self._job_section(4, "Relevant Opportunities", d.by_category(RelevanceCategory.RELEVANT),
                          "Good matches; check the 'To verify' notes.")
        self._job_section(5, "Potentially Relevant Opportunities",
                          d.by_category(RelevanceCategory.POTENTIALLY_RELEVANT) +
                          d.by_category(RelevanceCategory.NEEDS_MANUAL_REVIEW),
                          "Partial matches or listings with missing information (salary, arrangement, requirements, "
                          "dates) that need checking.")
        self._section6()
        self._section7()
        self._section8()
        self._section9()
        self._appendix()
        return self.doc

    def save(self, path: Path) -> Path:
        try:
            self.build().save(str(path))
        except Exception as exc:  # noqa: BLE001
            raise ReportGenerationError(f"DOCX generation failed: {exc}") from exc
        return path
