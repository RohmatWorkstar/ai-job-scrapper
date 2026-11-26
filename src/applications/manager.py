"""Application manager: Basic and AI application flows, from scraped jobs to reviewable records.

Nothing here sends e-mail. Records end in PENDING_APPROVAL (or REJECTED/SKIPPED/FAILED with a
reason); sending is done by EmailService / SendingScheduler after approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.ai.provider_manager import AIManager
from src.applications.contacts import select_recipient
from src.applications.cv_generator import CVGenerationError, CVGenerator
from src.applications.cv_matcher import CVMatcher
from src.applications.eligibility import EligibilityFilter
from src.applications.email_generator import EmailGenerator
from src.applications.email_service import AttachmentError, validate_attachment
from src.applications.job_analyzer import JobAnalyzer
from src.applications.models import ApplicationChannel, ApplicationMode, ApplicationRecord, ApplicationStatus, file_sha256
from src.applications.tracker import ApplicationTracker, DuplicateApplicationError, InvalidTransitionError, make_dedupe_key
from src.core.configuration import Settings
from src.core.models import JobOpportunity, SearchConfig
from src.cv.document import CVDocument, structure_cv, structure_cv_ai
from src.cv.models import CandidateProfile, ParsedCV
from src.discovery.query_generator import Preferences
from src.storage.database import Database
from src.storage.repository import Repository
from src.utils.logger import get_logger, log_event

log = get_logger(__name__)
S = ApplicationStatus


@dataclass
class PrepareSummary:
    considered: int = 0
    prepared: list[ApplicationRecord] = field(default_factory=list)
    rejected: list[tuple[JobOpportunity, list[str]]] = field(default_factory=list)
    no_email: int = 0
    no_url: int = 0
    previously_closed: int = 0
    invalid_email: list[tuple[JobOpportunity, list[str]]] = field(default_factory=list)
    duplicates: list[tuple[JobOpportunity, ApplicationRecord]] = field(default_factory=list)
    failed: list[tuple[JobOpportunity, str]] = field(default_factory=list)


class ApplicationManager:
    def __init__(self, settings: Settings, db: Database, *, ai: AIManager | None = None,
                 config: SearchConfig | None = None, preferences: Preferences | None = None) -> None:
        self.settings = settings
        self.db = db
        self.repo = Repository(db)
        self.tracker = ApplicationTracker(
            db, recipient_cooldown_days=float(settings.get("applications.recipient_cooldown_days", 30)))
        self.ai = ai
        self.config = config
        self.preferences = preferences
        self.allow_freemail = bool(settings.get("applications.allow_freemail_recipients", True))
        self.max_attachment = int(float(settings.get("applications.sending.max_attachment_mb", 10)) * 1024 * 1024)
        self.min_match = float(settings.get("applications.ai.min_match_score", 55))
        self.emails = EmailGenerator(settings, ai)
        self.cv_generator = CVGenerator(settings, ai)
        self.analyzer = JobAnalyzer(settings, ai)
        self.cv: ParsedCV | None = None
        self.profile: CandidateProfile | None = None
        self.doc: CVDocument | None = None
        self.cv_path: Path | None = None

    # -------------------------------------------------------------- inputs
    async def set_candidate(self, cv: ParsedCV, profile: CandidateProfile, cv_path: Path, *, use_ai: bool) -> None:
        self.cv, self.profile, self.cv_path = cv, profile, cv_path
        self.doc = await structure_cv_ai(cv, profile, self.ai) if use_ai else structure_cv(cv, profile)

    def load_jobs(self, session_id: str | None = None, *,
                  all_recent_days: int | None = None) -> tuple[str | None, list[JobOpportunity]]:
        if all_recent_days is not None:
            jobs = self.repo.recent_opportunities(days=all_recent_days)
            return f"recent-{all_recent_days}d", jobs
        if session_id is None:
            sessions = [s for s in self.repo.list_sessions(20) if s.get("status") != "running"]
            if not sessions:
                return None, []
            session_id = sessions[0]["id"]
        elif self.repo.get_session(session_id) is None:
            raise ValueError(f"Unknown session id: {session_id}")
        jobs = self.repo.session_opportunities(session_id)
        jobs.sort(key=lambda j: -(j.relevance_score or 0))
        return session_id, jobs

    def _job_url(self, job: JobOpportunity) -> str:
        return job.application_url or job.source_url

    def _already_closed(self, job: JobOpportunity, *, mode: str | None = None, channel: str = "email",
                        session_id: str | None = None) -> bool:
        """True if user explicitly closed this job (any mode/channel), or system closed it for the
        exact same (job_key, mode, channel, session_id)."""
        user_row = self.db.conn.execute(
            "SELECT 1 FROM applications WHERE job_key=? AND status IN ('REJECTED','SKIPPED') "
            "AND closed_by='user' LIMIT 1",
            (job.job_id,),
        ).fetchone()
        if user_row is not None:
            return True
        if mode is None:
            return False
        sys_row = self.db.conn.execute(
            "SELECT 1 FROM applications WHERE job_key=? AND status IN ('REJECTED','SKIPPED') "
            "AND (closed_by='system' OR closed_by IS NULL) AND mode=? AND channel=? "
            "AND ((session_id IS NULL AND ? IS NULL) OR session_id = ?) LIMIT 1",
            (job.job_id, mode, channel, session_id, session_id),
        ).fetchone()
        return sys_row is not None

    def _base_record(self, job: JobOpportunity, mode: ApplicationMode, recipient: str | None,
                     session_id: str | None, *, channel: str = ApplicationChannel.EMAIL.value) -> dict[str, Any]:
        return dict(
            mode=mode.value,
            channel=channel,
            sender_name=(self.doc.identity.name or None) if self.doc else None,
            job_key=job.job_id,
            session_id=session_id,
            company_name=job.company_name,
            job_title=job.job_title,
            job_url=self._job_url(job),
            recipient_email=recipient,
            original_cv_path=str(self.cv_path) if self.cv_path else None,
            original_cv_sha256=self.cv.sha256 if self.cv else None,
        )

    # --------------------------------------------------------------- basic
    def prepare_basic(self, jobs: list[JobOpportunity], cv_pdf: Path, *, session_id: str | None = None,
                      limit: int | None = None, eligibility_overrides: dict[str, Any] | None = None,
                      allow_duplicate: bool = False, authorization: str | None = None) -> PrepareSummary:
        if self.profile is None or self.doc is None:
            raise ValueError("set_candidate() must be called before preparing applications")
        try:
            validate_attachment(cv_pdf, self.max_attachment)
        except AttachmentError as exc:
            raise AttachmentError(f"Basic mode needs your original CV as a PDF: {exc}") from exc
        cv_sha = file_sha256(str(cv_pdf))
        filt = EligibilityFilter(self.settings, eligibility_overrides,
                                candidate_seniority=self.profile.seniority_estimate)
        summary = PrepareSummary()
        limit = limit or int(self.settings.get("applications.max_jobs_per_run", 30))
        for job in jobs:
            if len(summary.prepared) >= limit:
                break
            summary.considered += 1
            if not job.application_emails:
                summary.no_email += 1
                continue
            rc = select_recipient(job, own_email=self.doc.identity.email, allow_freemail=self.allow_freemail)
            dedupe_key = make_dedupe_key(job.company_name, job.job_title, rc.email, self._job_url(job))
            existing = self.tracker.find_blocking(dedupe_key, recipient=rc.email if rc.valid else None)
            if existing and not allow_duplicate:
                summary.duplicates.append((job, existing))
                continue
            if self._already_closed(job, mode=ApplicationMode.BASIC.value, channel="email", session_id=session_id):
                summary.previously_closed += 1
                continue
            elig = filt.evaluate(job)
            base = self._base_record(job, ApplicationMode.BASIC, rc.email, session_id, channel="email")
            if not rc.valid:
                summary.invalid_email.append((job, rc.reasons))
                rec = self._create(base, allow_duplicate, authorization, notes=rc.reasons)
                if rec:
                    self.tracker.skip(rec.id, "; ".join(rc.reasons)[:500], closed_by="system")
                continue
            if not elig.eligible:
                summary.rejected.append((job, elig.reasons))
                rec = self._create(base, allow_duplicate, authorization, notes=elig.reasons,
                                   match_score=job.relevance_score)
                if rec:
                    self.tracker.reject(rec.id, "not eligible: " + "; ".join(elig.reasons)[:500], closed_by="system")
                continue
            analysis = self.analyzer.analyze_deterministic(job)  # no AI call: shown so the user knows the job
            email = self.emails.template_email(job, self.profile, self.doc.identity,
                                               matched_skills=job.match.matched_skills, doc=self.doc,
                                               recipient_email=rc.email, analysis=analysis)
            rec = self._create(base, allow_duplicate, authorization,
                               notes=rc.warnings + email.notes + self.identity_warnings(),
                               match_score=job.relevance_score,
                               match={"relevance_category": job.relevance_category, "eligibility": elig.passed,
                                      "explanation": job.match.explanation[:6], "email_method": email.method,
                                      "verification_status": job.verification_status,
                                      "requirements": analysis.requirements[:6],
                                      "responsibilities": analysis.responsibilities[:4],
                                      "description_excerpt": (job.job_description or "")[:400]},
                               email_subject=email.subject, email_body=email.body,
                               attachment_path=str(cv_pdf), attachment_sha256=cv_sha)
            if rec is None:
                summary.duplicates.append((job, self.tracker.find_blocking(rec_key(job, rc.email))))  # type: ignore[arg-type]
                continue
            summary.prepared.append(self.tracker.transition(rec.id, S.PENDING_APPROVAL, "basic application prepared"))
        log_event(log, 20, "applications.basic_prepared", prepared=len(summary.prepared), no_email=summary.no_email)
        return summary

    def identity_warnings(self) -> list[str]:
        ident = self.doc.identity if self.doc else None
        if ident is None:
            return []
        missing = [k for k, v in (("name", ident.name), ("phone", ident.phone), ("e-mail", ident.email)) if not v]
        if not missing:
            return []
        return [f"Signature has no {', '.join(missing)} (not found in the CV header). Set APPLICANT_NAME / "
                "APPLICANT_PHONE / APPLICANT_EMAIL in .env or edit the e-mail."]

    def _create(self, base: dict[str, Any], allow_duplicate: bool, authorization: str | None,
                **extra: Any) -> ApplicationRecord | None:
        try:
            return self.tracker.create(allow_duplicate=allow_duplicate, duplicate_authorization=authorization,
                                       **base, **extra)
        except DuplicateApplicationError:
            return None

    # ------------------------------------------------------------------ AI
    async def prepare_ai(self, job: JobOpportunity, *, session_id: str | None = None, allow_duplicate: bool = False,
                         authorization: str | None = None) -> ApplicationRecord | None:
        """Analyse, match, generate a tailored CV and e-mail. Returns None when the job is skipped early."""
        if self.profile is None or self.doc is None:
            raise ValueError("set_candidate() must be called before preparing applications")
        rc = select_recipient(job, own_email=self.doc.identity.email, allow_freemail=self.allow_freemail)
        if not rc.valid or self._already_closed(job, mode=ApplicationMode.AI.value, channel="email",
                                                session_id=session_id):
            return None
        base = self._base_record(job, ApplicationMode.AI, rc.email, session_id, channel="email")
        rec = self.tracker.create(allow_duplicate=allow_duplicate, duplicate_authorization=authorization,
                                  notes=list(rc.warnings), **base)
        try:
            analysis = await self.analyzer.analyze(job)
            self.tracker.update_fields(rec.id, match={"analysis": analysis.to_dict()})
            self.tracker.transition(rec.id, S.ANALYZED, f"job analysed ({analysis.method})")
            match = CVMatcher(self.settings, self.profile, self.config, self.preferences).match(job, analysis, self.doc)
            info = {"analysis": analysis.to_dict(), "match": match.to_dict(), "cv_structure": self.doc.method}
            self.tracker.update_fields(rec.id, match=info, match_score=float(match.overall_score))
            if match.overall_score < self.min_match:
                return self.tracker.reject(
                    rec.id, f"match score {match.overall_score} below {self.min_match:g}", closed_by="system"
                )
            self.tracker.transition(rec.id, S.MATCHED, f"match score {match.overall_score}")
            tcv, pdf = await self.cv_generator.generate(
                self.doc, analysis, match, company=job.company_name, title=job.job_title, app_id=rec.id,
                experience_years=self.profile.experience_years,
            )
            matched = [m["skill"] for m in match.matched_skills]
            email = await self.emails.ai_email(job, analysis, self.profile, self.doc, matched,
                                               recipient_email=rc.email)
            info.update({"cv_changes": tcv.changes, "rejected_rewrites": tcv.rejected_rewrites,
                         "cv_method": tcv.method, "email_method": email.method})
            self.tracker.update_fields(rec.id, match=info, notes=rec.notes + email.notes + self.identity_warnings())
            self.tracker.set_content(rec.id, subject=email.subject, body=email.body, attachment_path=str(pdf),
                                     attachment_sha256=file_sha256(str(pdf)), generated_cv_path=str(pdf))
            return self.tracker.transition(rec.id, S.PENDING_APPROVAL, "AI application prepared")
        except CVGenerationError as exc:
            return self.tracker.mark_failed(rec.id, error=str(exc), error_kind="cv_generation", transport="none")
        except Exception as exc:  # noqa: BLE001 - keep the record and the reason
            log_event(log, 40, "applications.ai_failed", id=rec.id, error=repr(exc))
            return self.tracker.mark_failed(rec.id, error=f"{type(exc).__name__}: {exc}", error_kind="preparation",
                                            transport="none")
        except BaseException:
            # Ctrl+C / cancellation: never leave a half-prepared record in a duplicate-blocking state.
            try:
                self.tracker.mark_failed(rec.id, error="Preparation interrupted", error_kind="interrupted",
                                         transport="none")
            except Exception:  # noqa: BLE001
                pass
            raise

    # ------------------------------------------------------------ Web/ATS assist
    async def prepare_web(
        self,
        job: JobOpportunity,
        *,
        mode: ApplicationMode = ApplicationMode.AI,
        cv_pdf: Path | None = None,
        session_id: str | None = None,
        allow_duplicate: bool = False,
        authorization: str | None = None,
        eligibility_overrides: dict[str, Any] | None = None,
    ) -> ApplicationRecord | None:
        """Prepare a Web/ATS assist application pack (tailored or original PDF CV + cover note)."""
        if self.profile is None or self.doc is None:
            raise ValueError("set_candidate() must be called before preparing applications")
        url = self._job_url(job)
        if not url:
            return None
        if self._already_closed(job, mode=mode.value, channel="web", session_id=session_id):
            return None
        overrides = dict(eligibility_overrides or {})
        overrides["require_email"] = False
        if mode == ApplicationMode.AI:
            overrides.setdefault("min_relevance_score", 0)
            overrides.setdefault("allowed_relevance_categories", [])
        filt = EligibilityFilter(self.settings, overrides, candidate_seniority=self.profile.seniority_estimate)
        elig = filt.evaluate(job)
        base = self._base_record(job, mode, None, session_id, channel="web")
        if not elig.eligible:
            rec = self.tracker.create(
                allow_duplicate=allow_duplicate, duplicate_authorization=authorization,
                notes=elig.reasons, match_score=job.relevance_score, **base,
            )
            return self.tracker.reject(rec.id, "not eligible: " + "; ".join(elig.reasons)[:500], closed_by="system")

        if mode == ApplicationMode.BASIC:
            if cv_pdf is None:
                raise AttachmentError("Basic web mode needs your original CV as a PDF.")
            validate_attachment(cv_pdf, self.max_attachment)
            cv_sha = file_sha256(str(cv_pdf))
            analysis = self.analyzer.analyze_deterministic(job)
            note = await self.emails.cover_note(
                job, analysis, self.profile, self.doc, list(job.match.matched_skills), use_ai=False
            )
            rec = self.tracker.create(
                allow_duplicate=allow_duplicate,
                duplicate_authorization=authorization,
                notes=note.notes,
                match_score=job.relevance_score,
                match={
                    "relevance_category": job.relevance_category,
                    "eligibility": elig.passed,
                    "explanation": job.match.explanation[:6],
                    "email_method": note.method,
                    "verification_status": job.verification_status,
                    "requirements": analysis.requirements[:6],
                    "responsibilities": analysis.responsibilities[:4],
                    "description_excerpt": (job.job_description or "")[:400],
                },
                email_subject=note.subject,
                email_body=note.body,
                attachment_path=str(cv_pdf),
                attachment_sha256=cv_sha,
                **base,
            )
            return self.tracker.transition(rec.id, S.PENDING_APPROVAL, "web assist application prepared (basic)")

        # AI mode web assist: analyze -> match -> tailored CV -> cover note
        rec = self.tracker.create(allow_duplicate=allow_duplicate, duplicate_authorization=authorization, **base)
        try:
            analysis = await self.analyzer.analyze(job)
            self.tracker.update_fields(rec.id, match={"analysis": analysis.to_dict()})
            self.tracker.transition(rec.id, S.ANALYZED, f"job analysed ({analysis.method})")
            match = CVMatcher(self.settings, self.profile, self.config, self.preferences).match(job, analysis, self.doc)
            info = {"analysis": analysis.to_dict(), "match": match.to_dict(), "cv_structure": self.doc.method}
            self.tracker.update_fields(rec.id, match=info, match_score=float(match.overall_score))
            if match.overall_score < self.min_match:
                return self.tracker.reject(
                    rec.id, f"match score {match.overall_score} below {self.min_match:g}", closed_by="system"
                )
            self.tracker.transition(rec.id, S.MATCHED, f"match score {match.overall_score}")
            tcv, pdf = await self.cv_generator.generate(
                self.doc, analysis, match, company=job.company_name, title=job.job_title, app_id=rec.id,
                experience_years=self.profile.experience_years,
            )
            matched = [m["skill"] for m in match.matched_skills]
            note = await self.emails.cover_note(job, analysis, self.profile, self.doc, matched, use_ai=True)
            info.update({
                "cv_changes": tcv.changes,
                "rejected_rewrites": tcv.rejected_rewrites,
                "cv_method": tcv.method,
                "email_method": note.method,
            })
            self.tracker.update_fields(rec.id, match=info, notes=rec.notes + note.notes)
            self.tracker.set_content(
                rec.id,
                subject=note.subject,
                body=note.body,
                attachment_path=str(pdf),
                attachment_sha256=file_sha256(str(pdf)),
                generated_cv_path=str(pdf),
            )
            return self.tracker.transition(rec.id, S.PENDING_APPROVAL, "web assist application prepared (AI)")
        except CVGenerationError as exc:
            return self.tracker.mark_failed(rec.id, error=str(exc), error_kind="cv_generation", transport="none")
        except Exception as exc:  # noqa: BLE001
            log_event(log, 40, "applications.web_failed", id=rec.id, error=repr(exc))
            return self.tracker.mark_failed(
                rec.id, error=f"{type(exc).__name__}: {exc}", error_kind="preparation", transport="none"
            )
        except BaseException:
            try:
                self.tracker.mark_failed(
                    rec.id, error="Preparation interrupted", error_kind="interrupted", transport="none"
                )
            except Exception:  # noqa: BLE001
                pass
            raise

    async def prepare_batch(
        self,
        jobs: list[JobOpportunity],
        *,
        mode: ApplicationMode,
        channel: str = "email",
        cv_pdf: Path | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        allow_duplicate: bool = False,
        authorization: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> PrepareSummary:
        """Multi-channel batch preparation honoring shared cap for channel='all'."""
        if self.profile is None or self.doc is None:
            raise ValueError("set_candidate() must be called before preparing applications")
        if channel == "email" and mode == ApplicationMode.BASIC:
            if cv_pdf is None:
                raise AttachmentError("Basic mode needs your original CV as a PDF.")
            return self.prepare_basic(
                jobs, cv_pdf, session_id=session_id, limit=limit,
                allow_duplicate=allow_duplicate, authorization=authorization,
            )
        if mode == ApplicationMode.BASIC and cv_pdf is not None:
            validate_attachment(cv_pdf, self.max_attachment)
        default_cap = int(
            self.settings.get(
                "applications.ai.max_jobs_per_run" if mode == ApplicationMode.AI else "applications.max_jobs_per_run",
                10 if mode == ApplicationMode.AI else 30,
            )
        )
        max_jobs = limit or default_cap
        sorted_jobs = sorted(jobs, key=lambda j: -(j.relevance_score or 0))
        summary = PrepareSummary()
        pre_email_ai = EligibilityFilter(
            self.settings,
            {"min_relevance_score": 0, "allowed_relevance_categories": [], "require_email": True},
            candidate_seniority=self.profile.seniority_estimate,
        )
        pre_web_ai = EligibilityFilter(
            self.settings,
            {"min_relevance_score": 0, "allowed_relevance_categories": [], "require_email": False},
            candidate_seniority=self.profile.seniority_estimate,
        )
        for job in sorted_jobs:
            if len(summary.prepared) >= max_jobs:
                break
            summary.considered += 1
            # Decide target channel for this job.
            job_channel = channel
            if channel == "all":
                rc_probe = (
                    select_recipient(job, own_email=self.doc.identity.email, allow_freemail=self.allow_freemail)
                    if job.application_emails
                    else None
                )
                job_channel = "email" if (rc_probe is not None and rc_probe.valid) else "web"

            if job_channel == "email":
                if not job.application_emails:
                    summary.no_email += 1
                    continue
                if self._already_closed(job, mode=mode.value, channel="email", session_id=session_id):
                    summary.previously_closed += 1
                    continue
                if mode == ApplicationMode.BASIC:
                    assert cv_pdf is not None
                    sub = self.prepare_basic(
                        [job], cv_pdf, session_id=session_id, limit=1,
                        allow_duplicate=allow_duplicate, authorization=authorization,
                    )
                    summary.prepared.extend(sub.prepared)
                    summary.rejected.extend(sub.rejected)
                    summary.invalid_email.extend(sub.invalid_email)
                    summary.duplicates.extend(sub.duplicates)
                    summary.failed.extend(sub.failed)
                    summary.previously_closed += sub.previously_closed
                    continue
                res = pre_email_ai.evaluate(job)
                if not res.eligible:
                    summary.rejected.append((job, res.reasons))
                    continue
                rc = select_recipient(job, own_email=self.doc.identity.email, allow_freemail=self.allow_freemail)
                if not rc.valid:
                    summary.invalid_email.append((job, rc.reasons))
                    continue
                if on_progress:
                    on_progress(f"Analysing (email): {job.job_title} @ {job.company_name or '?'}")
                try:
                    rec = await self.prepare_ai(
                        job, session_id=session_id, allow_duplicate=allow_duplicate, authorization=authorization
                    )
                except DuplicateApplicationError as exc:
                    summary.duplicates.append((job, exc.existing))
                    continue
                if rec is None:
                    summary.previously_closed += 1
                elif rec.status == S.PENDING_APPROVAL.value:
                    summary.prepared.append(rec)
                elif rec.status == S.REJECTED.value:
                    summary.rejected.append((job, [f"AI match score {rec.match_score:g} below the threshold"]))
                else:
                    summary.failed.append((job, rec.error or rec.status))
                continue

            # Web/ATS channel
            if not self._job_url(job):
                summary.no_url += 1
                continue
            if self._already_closed(job, mode=mode.value, channel="web", session_id=session_id):
                summary.previously_closed += 1
                continue
            if mode == ApplicationMode.AI:
                res = pre_web_ai.evaluate(job)
                if not res.eligible:
                    summary.rejected.append((job, res.reasons))
                    continue
            if on_progress:
                on_progress(f"Preparing (web): {job.job_title} @ {job.company_name or '?'}")
            try:
                rec = await self.prepare_web(
                    job,
                    mode=mode,
                    cv_pdf=cv_pdf,
                    session_id=session_id,
                    allow_duplicate=allow_duplicate,
                    authorization=authorization,
                )
            except DuplicateApplicationError as exc:
                summary.duplicates.append((job, exc.existing))
                continue
            if rec is None:
                summary.previously_closed += 1
            elif rec.status == S.PENDING_APPROVAL.value:
                summary.prepared.append(rec)
            elif rec.status == S.REJECTED.value:
                msg = (
                    f"AI match score {rec.match_score:g} below the threshold"
                    if mode == ApplicationMode.AI and rec.match_score is not None and rec.match_score < self.min_match
                    else (rec.notes[0] if rec.notes else "not eligible")
                )
                summary.rejected.append((job, [msg]))
            else:
                summary.failed.append((job, rec.error or rec.status))
        return summary

    async def regenerate_cv(self, app_id: str) -> ApplicationRecord:
        """Rebuild the tailored CV without the AI response cache. Withdraws any approval."""
        if self.profile is None or self.doc is None:
            raise ValueError("Load the original CV first (select it when asked) to regenerate a CV.")
        rec = self.tracker.get(app_id)
        if rec is None or rec.mode != ApplicationMode.AI.value:
            raise ValueError("Only AI-mode applications have a generated CV")
        job = self.repo.get_opportunity(rec.job_key or "")
        if job is None:
            raise ValueError("The job for this application is no longer in the database")
        analysis = await self.analyzer.analyze(job)
        match = CVMatcher(self.settings, self.profile, self.config, self.preferences).match(job, analysis, self.doc)
        if rec.send_lock or rec.status not in (S.PENDING_APPROVAL.value, S.APPROVED.value):
            raise ValueError(f"Application {rec.id} is {rec.status}; its CV can no longer be regenerated")
        previous_cache = self.cv_generator.cache_days
        self.cv_generator.cache_days = 0
        try:
            existing = Path(rec.generated_cv_path) if rec.generated_cv_path else None
            tcv, pdf = await self.cv_generator.generate(
                self.doc, analysis, match, company=job.company_name, title=job.job_title, app_id=rec.id,
                experience_years=self.profile.experience_years, existing_path=existing,
            )
        finally:
            self.cv_generator.cache_days = previous_cache
        info = dict(rec.match)
        info.update({"cv_changes": tcv.changes, "rejected_rewrites": tcv.rejected_rewrites, "cv_method": tcv.method})
        self.tracker.update_fields(rec.id, match=info)
        return self.tracker.set_content(rec.id, attachment_path=str(pdf), attachment_sha256=file_sha256(str(pdf)),
                                        generated_cv_path=str(pdf), reason="CV regenerated")

    def edit_email(self, app_id: str, subject: str, body: str) -> ApplicationRecord:
        return self.tracker.set_content(app_id, subject=subject, body=body, reason="e-mail edited")

    # ------------------------------------------------------- automatic mode
    def automatic_allowed(self, cli_flag: bool) -> tuple[bool, str]:
        s = self.settings.get("applications.sending", {}) or {}
        if not cli_flag:
            return False, "automatic sending needs the --auto flag"
        if str(s.get("mode", "manual")) != "automatic":
            return False, "applications.sending.mode is not 'automatic'"
        if not s.get("automatic_enabled", False):
            return False, "applications.sending.automatic_enabled is false"
        return True, ""

    def auto_approve(self, records: list[ApplicationRecord]) -> list[ApplicationRecord]:
        """Approve e-mail applications by policy (only used in explicitly enabled automatic mode)."""
        threshold = float(self.settings.get("applications.sending.automatic_min_match_score", 75))
        approved = []
        for r in records:
            fresh = self.tracker.get(r.id)
            if fresh is None or fresh.status != S.PENDING_APPROVAL.value:
                continue
            if fresh.channel == "web":
                raise InvalidTransitionError(
                    f"Application {fresh.id} is a Web/ATS application and cannot be auto-approved for e-mail sending."
                )
            if (fresh.match_score or 0) < threshold or any("free-mail" in n for n in fresh.notes):
                continue
            approved.append(self.tracker.approve(fresh.id, approved_by=f"automatic-policy(score>={threshold:g})"))
        return approved


def rec_key(job: JobOpportunity, recipient: str | None) -> str:
    return make_dedupe_key(job.company_name, job.job_title, recipient, job.application_url or job.source_url)
