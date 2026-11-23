"""Unit tests for the application-automation modules (offline; no Gmail, no AI network calls)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.applications.contacts import select_recipient, validate_address
from src.applications.cv_document import parse_experience, structure_cv
from src.applications.cv_generator import CVGenerator, custom_cv_filename, safe_component
from src.applications.cv_matcher import CVMatcher
from src.applications.eligibility import EligibilityFilter
from src.applications.email_generator import EmailGenerator
from src.applications.fact_check import check_free_text, check_rewrite
from src.applications.gmail_client import (
    SEND_URL,
    DeliveryUnknownError,
    GmailAuth,
    GmailAuthError,
    GmailClient,
    GmailPolicyError,
    GmailQuotaError,
    GmailRequestError,
    GmailSendingLimitError,
    GmailTransientError,
)
from src.applications.job_analyzer import JobAnalyzer, detect_language
from src.applications.models import ApplicationStatus, file_sha256
from src.applications.sending_limits import SendingGuard, SendingLimits
from src.applications.tracker import (
    ApplicationTracker,
    DuplicateApplicationError,
    InvalidTransitionError,
    make_dedupe_key,
)
from src.core.exceptions import ConfigurationError, CVError
from src.core.models import JobOpportunity, RawDiscovery, SourceType
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.extraction.contact_parser import find_emails
from src.extraction.html_extractor import extract_from_html
from src.extraction.content_parser import SkillDetector
from src.extraction.job_normalizer import JobNormalizer, enrich_with_page
from src.storage.database import Database
from src.utils.date_time import utc_now

S = ApplicationStatus


# ------------------------------------------------------------------ helpers
class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        # Tuesday 10:00 Asia/Jakarta (03:00 UTC): inside the default sending window.
        self.t = start or datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def make_pdf(path: Path, text: str = "Curriculum Vitae") -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "apps.sqlite3")
    yield d
    d.close()


@pytest.fixture
def tracker(db: Database) -> ApplicationTracker:
    return ApplicationTracker(db)


def new_record(tracker: ApplicationTracker, pdf: Path, *, company: str = "Acme", title: str = "Backend Developer",
               recipient: str = "jobs@acme.co.id", **kw: Any):
    return tracker.create(mode="basic", company_name=company, job_title=title, recipient_email=recipient,
                          email_subject=f"Application for {title}", email_body="Dear Hiring Team,\nHello.",
                          attachment_path=str(pdf), attachment_sha256=file_sha256(str(pdf)), **kw)


def job(**kw: Any) -> JobOpportunity:
    base = dict(job_id="j1", job_title="Backend Developer", company_name="Acme", relevance_score=70.0,
                relevance_category="RELEVANT", verification_status="LIKELY_OPEN", application_emails=["jobs@acme.co.id"],
                job_description="We build fintech APIs.", required_skills=["Node.js", "PostgreSQL"])
    base.update(kw)
    return JobOpportunity(**base)


# ------------------------------------------------------- e-mail extraction
def test_find_emails_ranks_application_context_and_deobfuscates() -> None:
    text = ("Questions? press@acme.com.\nKirim CV dan lamaran ke hrd@acme.co.id paling lambat 30 Nov.\n"
            "Or apply: jobs [at] acme [dot] com")
    out = find_emails(text)
    assert out == ["hrd@acme.co.id", "jobs@acme.com"]  # press@ ("Questions?") is not an application inbox


def test_find_emails_mailto_first_and_decoded() -> None:
    out = find_emails("contact sales@x.io", mailto=["mailto:careers%40x.io?subject=Apply"])
    assert out == ["careers@x.io"]  # sales@ has no application instruction


def test_html_extractor_collects_mailto_links() -> None:
    html = ('<html><body><h1>Backend Developer</h1><a href="mailto:jobs@acme.co.id">Apply by e-mail</a>'
            '<a href="/apply">Apply</a></body></html>')
    page = extract_from_html(html, "https://acme.co.id/careers/1")
    assert page.mailto_links == ["mailto:jobs@acme.co.id"]
    assert page.apply_links == ["https://acme.co.id/apply"]


def test_normalizer_keeps_email_beyond_description_truncation(settings) -> None:
    settings.raw["extraction"]["max_description_chars"] = 200
    long_text = "Backend developer role. " * 50 + "\nSend your CV to recruit@acme.co.id"
    d = RawDiscovery(source_type=SourceType.COMMUNITY, source_name="HN", source_url="https://news.ycombinator.com/x",
                     title="Acme | Backend Developer", retrieved_at=utc_now(), text=long_text)
    j = JobNormalizer(settings).normalise(d)
    assert len(j.job_description) <= 200 and "recruit@" not in j.job_description
    assert j.application_emails == ["recruit@acme.co.id"]
    assert any(e.field == "application_email" for e in j.evidence)


def test_enrich_with_page_takes_page_emails(settings) -> None:
    page = extract_from_html('<html><body><p>Lamaran ke hr@acme.co.id</p></body></html>', "https://acme.co.id/job")
    d = RawDiscovery(source_type=SourceType.COMPANY_CAREERS, source_name="Career page", source_url="https://acme.co.id/job",
                     title="Backend Developer", retrieved_at=utc_now())
    enrich_with_page(d, page)
    assert JobNormalizer(settings).normalise(d).application_emails == ["hr@acme.co.id"]


def test_old_opportunity_json_without_emails_still_loads() -> None:
    j = JobOpportunity.from_dict({"job_id": "x", "job_title": "Dev"})
    assert j.application_emails == []


# ---------------------------------------------------------------- recipients
@pytest.mark.parametrize("addr,ok", [
    ("jobs@acme.co.id", True), ("no-reply@acme.com", False), ("noreply@acme.com", False),
    ("jobs@linkedin.com", False), ("you@example.com", False), ("bad@@x.com", False), ("logo@2x.png", False),
])
def test_validate_address(addr: str, ok: bool) -> None:
    assert validate_address(addr)[0] is ok


def test_select_recipient_prefers_company_domain_and_flags_freemail() -> None:
    j = job(application_emails=["acme.hr@gmail.com", "talent@acme.co.id"], company_website="https://www.acme.co.id")
    rc = select_recipient(j)
    assert rc.valid and rc.email == "talent@acme.co.id"
    rc2 = select_recipient(job(application_emails=["acme.hr@gmail.com"]))
    assert rc2.valid and any("free-mail" in w for w in rc2.warnings)
    assert not select_recipient(job(application_emails=["acme.hr@gmail.com"]), allow_freemail=False).valid
    assert not select_recipient(job(application_emails=["me@acme.co.id"]), own_email="me@acme.co.id").valid
    missing = select_recipient(job(application_emails=[]))
    assert not missing.valid and "No application e-mail" in missing.reasons[0]


# --------------------------------------------------------------- eligibility
def test_eligibility_filter_rules(settings) -> None:
    f = EligibilityFilter(settings)
    assert f.evaluate(job()).eligible
    assert not f.evaluate(job(relevance_score=30, relevance_category="NOT RELEVANT")).eligible
    assert not f.evaluate(job(verification_status="POSSIBLE_SCAM")).eligible
    assert not f.evaluate(job(job_title="Sales Intern")).eligible
    assert not f.evaluate(job(application_emails=[])).eligible
    f2 = EligibilityFilter(settings, {"title_include_keywords": ["data"]})
    assert not f2.evaluate(job()).eligible


# ------------------------------------------------------------------- tracker
def test_tracker_lifecycle_duplicates_and_persistence(tmp_path: Path, tracker: ApplicationTracker, db: Database) -> None:
    pdf = make_pdf(tmp_path / "cv.pdf")
    rec = new_record(tracker, pdf)
    assert rec.status == "NEW" and rec.dedupe_key == make_dedupe_key("Acme", "Backend Developer")
    with pytest.raises(DuplicateApplicationError):
        new_record(tracker, pdf, company="ACME Ltd")  # normalised company + title collide
    with pytest.raises(DuplicateApplicationError):
        new_record(tracker, pdf, allow_duplicate=True)  # no recorded authorisation
    dup = new_record(tracker, pdf, allow_duplicate=True, duplicate_authorization="user confirmed")
    assert dup.allow_duplicate and dup.duplicate_authorization == "user confirmed"

    with pytest.raises(InvalidTransitionError):
        tracker.transition(rec.id, S.SENT)  # NEW -> SENT is never allowed
    with pytest.raises(InvalidTransitionError):
        tracker.approve(rec.id)  # only PENDING_APPROVAL can be approved
    tracker.transition(rec.id, S.PENDING_APPROVAL)
    approved = tracker.approve(rec.id)
    assert approved.approved_hash == approved.content_hash()
    edited = tracker.set_content(rec.id, body="Changed body")
    assert edited.status == "PENDING_APPROVAL" and edited.approved_hash is None and edited.approval_status == "none"

    # persistence across a restart (new connection)
    path = db.path
    db.close()
    db2 = Database(path)
    try:
        again = ApplicationTracker(db2).get(rec.id)
        assert again is not None and again.email_body == "Changed body"
        assert [e["to_status"] for e in ApplicationTracker(db2).events(rec.id)][:3] == ["NEW", "PENDING_APPROVAL", "APPROVED"]
    finally:
        db2.close()


def test_send_lock_claimed_only_once(tmp_path: Path, tracker: ApplicationTracker) -> None:
    pdf = make_pdf(tmp_path / "cv.pdf")
    rec = new_record(tracker, pdf)
    assert not tracker.claim_send_lock(rec.id)  # not approved
    tracker.transition(rec.id, S.PENDING_APPROVAL)
    tracker.approve(rec.id)
    assert tracker.claim_send_lock(rec.id)
    assert not tracker.claim_send_lock(rec.id)


def test_schema_version_is_2(db: Database) -> None:
    row = db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row["value"] == "2"


# ------------------------------------------------------------ sending limits
def _limits(**kw: Any) -> SendingLimits:
    base = dict(max_per_day=5, max_per_hour=3, min_interval_seconds=60, max_per_recipient_domain_per_day=2,
                window_start_hour=None, window_end_hour=None)
    base.update(kw)
    return SendingLimits(**base)


def test_limits_config_refuses_unsafe_values(settings) -> None:
    settings.raw["applications"]["sending"]["max_per_day"] = 400
    with pytest.raises(ConfigurationError):
        SendingLimits.from_settings(settings)
    settings.raw["applications"]["sending"]["account_type"] = "workspace"
    assert SendingLimits.from_settings(settings).max_per_day == 400  # within half of 2,000
    settings.raw["applications"]["sending"]["max_per_day"] = 100
    settings.raw["applications"]["sending"]["account_type"] = "personal"
    lim = SendingLimits.from_settings(settings)
    assert lim.max_per_day == 100 and lim.warnings()  # allowed, with a risk warning


def test_guard_interval_hourly_daily_and_domain(tracker: ApplicationTracker) -> None:
    clock = Clock()
    g = SendingGuard(tracker, _limits(), transport_name="mock", now=clock)
    assert g.check("a@one.com").allowed
    g.record("x", "sent", "a@one.com")
    d = g.check("b@two.com")
    assert d.code == "min_interval" and 0 < d.wait_seconds <= 60
    clock.advance(61)
    g.record("x", "sent", "b@one.com")
    clock.advance(61)
    assert g.check("c@one.com").code == "domain_limit"
    g.record("x", "sent", "c@three.com")
    clock.advance(61)
    d = g.check("d@four.com")
    assert d.code == "hourly_limit" and d.wait_seconds > 0
    clock.advance(3600)
    g.record("x", "sent", "e@five.com")
    clock.advance(61)
    g.record("x", "sent", "f@six.com")
    clock.advance(3600)
    d = g.check("g@seven.com")
    assert d.code == "daily_limit"
    clock.advance(24 * 3600)
    assert g.check("g@seven.com").allowed  # rolling 24h window


def test_guard_counters_are_per_transport(tracker: ApplicationTracker) -> None:
    clock = Clock()
    real = SendingGuard(tracker, _limits(max_per_day=1), transport_name="gmail", now=clock)
    dry = SendingGuard(tracker, _limits(max_per_day=1), transport_name="dry_run", now=clock)
    dry.record("x", "sent", "a@a.com")
    assert real.check("b@b.com").allowed


def test_guard_sending_window(tracker: ApplicationTracker) -> None:
    clock = Clock(datetime(2026, 11, 28, 3, 0, tzinfo=timezone.utc))  # Saturday 10:00 Jakarta
    g = SendingGuard(tracker, _limits(window_start_hour=8, window_end_hour=18), transport_name="mock", now=clock)
    assert g.check().code == "outside_window"


def test_emergency_stop_and_resume(tracker: ApplicationTracker, tmp_path: Path) -> None:
    stop = tmp_path / "STOP_SENDING"
    g = SendingGuard(tracker, _limits(), transport_name="mock", stop_file=stop, now=Clock())
    g.emergency_stop("test")
    assert g.check().code == "emergency_stop"
    g.clear_emergency_stop()
    assert g.check().allowed
    stop.write_text("stop")
    assert g.check().code == "emergency_stop"
    g.clear_emergency_stop()
    assert not stop.exists() and g.check().allowed


def test_automatic_pause_on_consecutive_failures_and_invalid_recipients(tracker: ApplicationTracker) -> None:
    clock = Clock()
    g = SendingGuard(tracker, _limits(max_consecutive_failures=2, max_per_day=50, max_per_hour=50),
                     transport_name="mock", now=clock)
    g.record("1", "failed", "a@a.com", error_kind="transient")
    assert g.pause_state() is None
    g.record("2", "failed", "b@b.com", error_kind="transient")
    assert g.check().code == "paused"
    g.resume()
    g2 = SendingGuard(tracker, _limits(max_consecutive_failures=99, max_invalid_recipients_per_day=2, max_per_day=50,
                                       max_per_hour=50), transport_name="mock2", now=clock)
    g2.record("3", "failed", "x@a.com", error_kind="invalid_recipient")
    g2.record("4", "sent", "y@b.com")
    g2.record("5", "failed", "z@c.com", error_kind="invalid_recipient")
    assert g2.check().code == "paused"


def test_timed_pause_expires(tracker: ApplicationTracker) -> None:
    clock = Clock()
    g = SendingGuard(tracker, _limits(), transport_name="mock", now=clock)
    g.pause("quota", hours=24)
    d = g.check()
    assert d.code == "paused" and d.wait_seconds == pytest.approx(24 * 3600)
    clock.advance(24 * 3600 + 1)
    assert g.check().allowed


# ------------------------------------------------------------------- Gmail
def _gmail_error(status: int, reason: str, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message,
                                                  "errors": [{"reason": reason, "message": message}]}},
                          headers=headers)


def _client(responses: list[Any], tokens: list[bool] | None = None, attempts: int = 3) -> tuple[GmailClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def no_sleep(_: float) -> None:
        return None

    def token(force: bool) -> str:
        if tokens is not None:
            tokens.append(force)
        return "tok-new" if force else "tok"

    return GmailClient(token, transport=httpx.MockTransport(handler), max_attempts=attempts, sleep=no_sleep), seen


def test_gmail_send_success_uses_media_upload() -> None:
    client, seen = _client([httpx.Response(200, json={"id": "m1", "threadId": "t1"})])
    assert asyncio.run(client.send_raw(b"From: a\r\n\r\nhi")) == "m1"
    req = seen[0]
    assert str(req.url).startswith(SEND_URL) and req.url.params["uploadType"] == "media"
    assert req.headers["Content-Type"] == "message/rfc822" and req.headers["Authorization"] == "Bearer tok"


def test_gmail_mail_sending_limit_is_not_retried() -> None:
    client, seen = _client([_gmail_error(429, "rateLimitExceeded", "Too many requests: User-rate limit exceeded (Mail sending)",
                                         {"Retry-After": "3600"})])
    with pytest.raises(GmailSendingLimitError) as e:
        asyncio.run(client.send_raw(b"x"))
    assert len(seen) == 1 and e.value.pause_sending and e.value.retry_after == 3600


@pytest.mark.parametrize("status,reason,exc", [
    (403, "dailyLimitExceeded", GmailQuotaError), (403, "domainPolicy", GmailPolicyError),
    (400, "invalidArgument", GmailRequestError),
])
def test_gmail_permanent_errors_not_retried(status: int, reason: str, exc: type) -> None:
    client, seen = _client([_gmail_error(status, reason, reason)])
    with pytest.raises(exc):
        asyncio.run(client.send_raw(b"x"))
    assert len(seen) == 1


def test_gmail_rate_limit_and_5xx_retried_with_backoff() -> None:
    client, seen = _client([_gmail_error(403, "userRateLimitExceeded", "slow down"),
                            _gmail_error(503, "backendError", "backend"), httpx.Response(200, json={"id": "m2"})])
    assert asyncio.run(client.send_raw(b"x")) == "m2" and len(seen) == 3


def test_gmail_persistent_errors_after_max_attempts() -> None:
    client, seen = _client([_gmail_error(500, "backendError", "b")] * 3)
    with pytest.raises(GmailTransientError):
        asyncio.run(client.send_raw(b"x"))
    assert len(seen) == 3
    client, _ = _client([_gmail_error(429, "", "Too many concurrent requests for user")] * 3)
    with pytest.raises(GmailQuotaError):  # persistent rate limiting pauses sending
        asyncio.run(client.send_raw(b"x"))


def test_gmail_401_refreshes_token_once() -> None:
    tokens: list[bool] = []
    client, seen = _client([_gmail_error(401, "authError", "Invalid Credentials"), httpx.Response(200, json={"id": "m3"})],
                           tokens)
    assert asyncio.run(client.send_raw(b"x")) == "m3"
    assert True in tokens and seen[1].headers["Authorization"] == "Bearer tok"
    client, _ = _client([_gmail_error(401, "authError", "bad")] * 3)
    with pytest.raises(GmailAuthError):
        asyncio.run(client.send_raw(b"x"))


def test_gmail_network_errors() -> None:
    client, seen = _client([httpx.ConnectError("down"), httpx.Response(200, json={"id": "m4"})])
    assert asyncio.run(client.send_raw(b"x")) == "m4" and len(seen) == 2
    client, seen = _client([httpx.ReadTimeout("slow")])
    with pytest.raises(DeliveryUnknownError):
        asyncio.run(client.send_raw(b"x"))
    assert len(seen) == 1  # never re-sent: the message may already be delivered


def test_gmail_auth_token_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = GmailAuth(tmp_path / "client.json", tmp_path / "token.json")
    with pytest.raises(GmailAuthError, match="--gmail-auth"):
        auth.access_token()
    token = {"token": "abc", "refresh_token": "r", "client_id": "cid", "client_secret": "cs",
             "token_uri": "https://oauth2.googleapis.com/token",
             "scopes": ["https://www.googleapis.com/auth/gmail.send"], "expiry": "2099-01-01T00:00:00Z"}
    (tmp_path / "token.json").write_text(json.dumps(token))
    assert auth.access_token() == "abc"
    # Expired + refresh failure (e.g. a 7-day "Testing" token) -> clear re-auth message.
    token["expiry"] = "2000-01-01T00:00:00Z"
    (tmp_path / "token.json").write_text(json.dumps(token))
    auth2 = GmailAuth(tmp_path / "client.json", tmp_path / "token.json")
    from google.oauth2.credentials import Credentials

    def boom(self, request):  # noqa: ANN001
        from google.auth.exceptions import RefreshError

        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(GmailAuthError, match="refresh failed"):
        auth2.access_token()
    with pytest.raises(GmailAuthError, match="OAuth client file not found"):
        auth2.authorize_interactive(open_browser=False)


# -------------------------------------------------------------- CV and AI
@pytest.fixture
def cv_doc(settings, sample_cv_path):
    cv = parse_cv(sample_cv_path)
    profile = CVAnalyzer(settings).analyze(cv)
    return cv, profile, structure_cv(cv, profile)


def test_structure_cv_from_sample(cv_doc) -> None:
    _, _, doc = cv_doc
    assert doc.identity.name == "Andi Pratama" and doc.identity.email == "andi.pratama@example.com"
    assert [e.title for e in doc.experience] == ["Fullstack Developer", "Backend Developer"]
    assert doc.experience[0].company == "PT Contoh Digital Nusantara (Fintech)"
    assert doc.experience[0].dates == "Jan 2022 - Present" and len(doc.experience[0].bullets) == 3
    assert "REST API" in doc.skills and "Jest" in doc.skills
    safe = json.dumps(doc.ai_safe_dict())
    assert "andi.pratama@example.com" not in safe and "Andi Pratama" not in safe and "+62" not in safe


def test_parse_experience_company_first_and_continuations() -> None:
    entries = parse_experience("Acme Corp | Software Engineer\n2019 - 2021\n- Built APIs for\npayments.\n")
    assert entries[0].title == "Software Engineer" and entries[0].company == "Acme Corp"
    assert entries[0].bullets == ["Built APIs for payments."]


def test_doc_cv_needs_converter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "old.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    monkeypatch.setattr("src.cv.parser.shutil.which", lambda name: None)
    with pytest.raises(CVError, match="antiword or LibreOffice"):
        parse_cv(p)


def test_ai_segmentation_must_be_verbatim(settings, cv_doc) -> None:
    from src.applications.cv_document import structure_cv_ai

    cv, profile, _ = cv_doc
    ai, _ = fake_ai({"Segment the work-experience": {"experience": [
        {"title": "Fullstack Developer", "company": "PT Contoh Digital Nusantara (Fintech)", "dates": "Jan 2022 - Present",
         "bullets": ["Built and maintained React + TypeScript dashboards used by 20k merchants.",
                     "Designed REST APIs with Node.js, Express and Prisma on PostgreSQL.",
                     "Introduced Docker-based local environments and GitHub Actions CI/CD.",
                     "Led a team of 15 engineers building React dashboards."]},   # fabricated
        {"title": "Backend Developer", "company": "CV Toko Daring Sejahtera (E-commerce)",
         "bullets": ["Developed Laravel (PHP) services and MySQL schemas for order management.",
                     "Integrated payment gateways and built MongoDB-based reporting jobs."]}]}})
    doc = asyncio.run(structure_cv_ai(cv, profile, ai))
    bullets = [b for e in doc.experience for b in e.bullets]
    assert not any("Led a team" in b for b in bullets) and len(bullets) == 5
    assert any("discarded" in n for n in doc.notes)


def test_email_cannot_claim_technologies_from_the_job_title(settings, cv_doc) -> None:
    _, profile, doc = cv_doc
    j = job(job_title="Golang Kubernetes Engineer", company_name="Acme")
    a = JobAnalyzer(settings).analyze_deterministic(j)
    body = ("Dear Hiring Team,\n\nI am applying for the Golang Kubernetes Engineer role at Acme. I have strong "
            "experience with Kubernetes and Golang from building REST APIs with Node.js on PostgreSQL. My CV is "
            "attached and I would welcome the chance to talk about the position with your team.")
    ai, _ = fake_ai({"job application e-mail": {"subject": "Golang Kubernetes Engineer application", "body": body}})
    out = asyncio.run(EmailGenerator(settings, ai).ai_email(j, a, profile, doc, ["Node.js"]))
    assert out.method == "template" and any("Kubernetes" in n or "Go" in n for n in out.notes)
    det = SkillDetector(settings.taxonomy)
    assert check_free_text("I bring five years of experience.", "Node.js developer", det)  # number words checked


def test_footer_mailto_does_not_outrank_application_address() -> None:
    html = ('<html><body><main><p>Send your CV to hr@acme.co.id</p></main>'
            '<footer><a href="mailto:info@jobportal-xyz.id">Contact</a></footer></body></html>')
    page = extract_from_html(html, "https://jobportal-xyz.id/job/1")
    assert page.mailto_links == []
    assert find_emails('Send your CV to hr@acme.co.id', mailto=['mailto:info@jobportal-xyz.id'])[0] == "hr@acme.co.id"


def test_sending_window_validation(settings) -> None:
    for window in ({"start_hour": 18, "end_hour": 8}, {"start_hour": "8", "end_hour": 18},
                   {"start_hour": 8, "end_hour": 18, "weekdays": []}, {"start_hour": 8, "end_hour": 18, "weekdays": [7]}):
        settings.raw["applications"]["sending"]["sending_window"] = window
        with pytest.raises(ConfigurationError):
            SendingLimits.from_settings(settings)


def test_settings_validation_and_setup_checklist(settings, tmp_path: Path) -> None:
    from src.applications.setup_check import setup_checklist, validate_applications_settings

    validate_applications_settings(settings)  # shipped defaults are valid
    settings.raw["applications"]["eligibility"]["min_relevance_score"] = "high"
    settings.raw["applications"]["eligibility"]["allowed_relevance_categories"] = ["GREAT"]
    settings.raw["applications"]["sending"]["mode"] = "turbo"
    with pytest.raises(ConfigurationError) as e:
        validate_applications_settings(settings)
    msg = str(e.value)
    assert "min_relevance_score" in msg and "GREAT" in msg and "mode" in msg
    settings.raw["candidate"]["cv_directory"] = str(tmp_path / "empty")
    items = {i.name: i for i in setup_checklist(settings, jobs_with_email=0)}
    assert not items["CV in cv/ folder"].ok and items["CV in cv/ folder"].fix
    assert not items["Scraped jobs with an application e-mail"].ok
    assert not items["Gmail authorisation token"].ok and "--gmail-auth" in items["Gmail authorisation token"].fix


def test_template_without_company_or_name_reads_naturally(settings, cv_doc) -> None:
    from src.applications.models import CandidateIdentity

    _, profile, doc = cv_doc
    e = EmailGenerator(settings).template_email(job(company_name=None), profile, CandidateIdentity(), doc=doc)
    assert e.body.startswith("Dear Hiring Team,") and "at your company," not in e.body.splitlines()[0]
    assert e.subject == "Application for Backend Developer"


def test_edited_email_validation() -> None:
    from src.applications.models import ApplicationRecord
    from src.cli.apply_cli import check_edited_email

    rec = ApplicationRecord(id="x", mode="basic", status="PENDING_APPROVAL", dedupe_key="k", created_at="",
                            updated_at="", job_title="Backend Developer", company_name="Acme")
    problems, _ = check_edited_email("", "short", rec)
    assert len(problems) == 2
    problems, _ = check_edited_email("Application", "Dear [Name], I want to apply for this role at your firm.", rec)
    assert any("placeholder" in p for p in problems)
    problems, warnings = check_edited_email("Application", "Dear team, I would like to apply for this position.", rec)
    assert problems == [] and len(warnings) == 2


def test_fact_check_rules(settings) -> None:
    det = SkillDetector(settings.taxonomy)
    cv = "Designed REST APIs with Node.js and PostgreSQL used by 20k merchants."
    src = "Designed REST APIs with Node.js and PostgreSQL used by 20k merchants."
    assert check_rewrite("Designed Node.js REST APIs on PostgreSQL serving 20k merchants.", src, cv, det) == []
    assert any("technologies" in p for p in check_rewrite("Designed Kubernetes-based Node.js APIs.", src, cv, det))
    assert any("numbers" in p for p in check_rewrite("Designed REST APIs used by 50k merchants.", src, cv, det))
    assert any("claims" in p for p in check_rewrite("Led a team that designed REST APIs with Node.js.", src, cv, det))
    assert check_free_text("Contact me at a@b.com", cv, det)
    assert check_free_text("Experienced with Node.js and PostgreSQL.", cv, det) == []


def test_job_analyzer_sections_and_ai_validation(settings) -> None:
    j = job(job_description="About us: we are a payments company serving merchants across Southeast Asia.\n"
                            "Responsibilities:\n- Build payment APIs in Node.js\n- Own CI pipelines\n"
                            "Requirements:\n- 3+ years with PostgreSQL\n- Docker\nBenefits:\n- Laptop\n"
                            "We review every application and reply within two weeks.")

    class FakeAI:
        available = True
        disabled_reason = ""

        async def ask_json(self, task, prompt, **kw):  # noqa: ANN001
            return {"required_skills": ["Node.js", "Rust"], "responsibilities": ["Build payment APIs in Node.js",
                                                                                 "Fly to Mars weekly"],
                    "keywords": ["payment", "blockchain"], "experience_years_required": 3}

    a = asyncio.run(JobAnalyzer(settings, FakeAI()).analyze(j))  # type: ignore[arg-type]
    assert a.responsibilities == ["Build payment APIs in Node.js"]
    assert "Rust" not in a.technologies and "blockchain" not in a.keywords
    assert a.experience_years_required == 3 and a.method == "deterministic+ai" and a.notes
    det = JobAnalyzer(settings).analyze_deterministic(j)
    assert det.requirements == ["3+ years with PostgreSQL", "Docker"] and "Laptop" not in det.requirements
    assert detect_language("Kami mencari pengembang yang memiliki pengalaman dan mampu bekerja") == "id"


def test_matcher_is_explainable_and_evidence_based(settings, cv_doc) -> None:
    _, profile, doc = cv_doc
    j = job(job_title="Backend Engineer (Node.js)", required_skills=["Node.js", "PostgreSQL", "Kubernetes"],
            preferred_skills=["Go"], job_description="3+ years experience with Node.js")
    a = JobAnalyzer(settings).analyze_deterministic(j)
    m = CVMatcher(settings, profile).match(j, a, doc)
    assert set(m.components) == {"skills", "title", "experience", "keywords"}
    assert m.missing_skills == ["Kubernetes"] and m.missing_preferred == ["Go"]
    assert {x["skill"] for x in m.matched_skills} == {"Node.js", "PostgreSQL"}
    assert 0 < m.overall_score <= 100 and m.relevant_experience and m.explanation


def test_cv_generator_deterministic_keeps_facts_and_renders_pdf(settings, cv_doc, tmp_path: Path) -> None:
    _, profile, doc = cv_doc
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")
    j = job(job_title="Backend Engineer", required_skills=["PostgreSQL", "Docker"])
    a = JobAnalyzer(settings).analyze_deterministic(j)
    m = CVMatcher(settings, profile).match(j, a, doc)
    gen = CVGenerator(settings)
    tcv, path = asyncio.run(gen.generate(doc, a, m, company="PT Acme / Indonesia", title="Backend Engineer (Go)", app_id="abc123"))
    assert path.name == "PT_Acme_Indonesia_Backend_Engineer_Go_Custom_CV.pdf" and path.parent == tmp_path / "gen"
    # same facts, only reordered; chronology and headline unchanged
    assert [e.title for e in tcv.experience] == [e.title for e in doc.experience]
    for a_e, d_e in zip(tcv.experience, doc.experience):
        assert sorted(a_e.bullets) == sorted(d_e.bullets)
    grouped = [i for _, items in doc.skill_groups for i in items]
    assert sorted(tcv.skills) == sorted(grouped) and tcv.skills[:2] == ["PostgreSQL", "Docker"]
    assert tcv.headline == "Fullstack Developer"
    assert gen.validate_pdf(path, tcv) == []
    assert file_sha256(str(path))
    assert custom_cv_filename(None, "Dev") == "Unknown_Dev_Custom_CV.pdf" and safe_component("Ünïcode ✓ Co.") == "Unicode_Co"


class FakeProvider:
    """AIProvider stand-in: answers by task, never touches the network."""

    name = "fake"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake-1"

    async def prepare(self) -> None:
        return None

    async def generate_json(self, system: str, prompt: str, *, max_output_tokens: int = 8192) -> Any:
        self.prompts.append(prompt)
        for key, ans in self.answers.items():
            if key in prompt:
                return ans
        return {}


def fake_ai(answers: dict[str, Any]):
    from src.ai.provider_manager import AIManager

    prov = FakeProvider(answers)
    return AIManager(prov, max_calls=20, cache_enabled=False), prov  # type: ignore[arg-type]


def test_cv_generator_ai_rewrites_validated(settings, cv_doc, tmp_path: Path) -> None:
    _, profile, doc = cv_doc
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path / "gen")
    ai, prov = fake_ai({"Tailor the candidate's CV": {
        "summary": "Fullstack developer with 10 years leading Kubernetes platforms.",  # fabricated -> rejected
        "skills_order": ["Docker", "Kubernetes", "Node.js"],                              # Kubernetes not in CV
        "bullets": [
            {"id": "e0b1", "text": "Designed REST APIs on PostgreSQL with Node.js, Express and Prisma."},  # ok
            {"id": "e0b0", "text": "Led a team of 12 building React dashboards for 20k merchants."},      # fabricated
        ]}})
    j = job(required_skills=["Docker", "Node.js"])
    a = JobAnalyzer(settings).analyze_deterministic(j)
    m = CVMatcher(settings, profile).match(j, a, doc)
    tcv = asyncio.run(CVGenerator(settings, ai).tailor(doc, a, m))
    bullets = [b for e in tcv.experience for b in e.bullets]
    assert "Designed REST APIs on PostgreSQL with Node.js, Express and Prisma." in bullets
    assert not any("Led a team" in b for b in bullets)
    assert tcv.summary == doc.summary
    assert "Kubernetes" not in tcv.skills and tcv.skills[0] == "Docker"
    assert {r["item"] for r in tcv.rejected_rewrites} == {"summary", "e0b0"}
    sent = prov.prompts[0]
    assert "andi.pratama@example.com" not in sent and "Andi Pratama" not in sent


def test_email_generator_template_and_ai_fallback(settings, cv_doc) -> None:
    _, profile, doc = cv_doc
    gen = EmailGenerator(settings)
    j = job(job_title="Backend Developer", company_name="Acme", required_skills=["Node.js", "Kubernetes"])
    e = gen.template_email(j, profile, doc.identity, matched_skills=["Node.js", "Kubernetes"], doc=doc)
    assert e.subject == "Application for Backend Developer - Andi Pratama"
    assert "Acme" in e.body and "Backend Developer" in e.body and "Node.js" in e.body
    assert "Kubernetes" not in e.body  # never claims a skill the CV does not prove
    assert e.body.rstrip().endswith("linkedin.com/in/andi-example")
    j_id = job(job_description="Kami mencari pengembang yang memiliki pengalaman dan mampu bekerja dalam tim.")
    assert gen.template_email(j_id, profile, doc.identity, doc=doc).subject.startswith("Lamaran Posisi")

    a = JobAnalyzer(settings).analyze_deterministic(j)
    bad_ai, _ = fake_ai({"job application e-mail": {"subject": "Backend Developer application", "body":
                         "Dear Hiring Team, I have 9 years of Kubernetes experience at Acme as Backend Developer. " * 4}})
    out = asyncio.run(EmailGenerator(settings, bad_ai).ai_email(j, a, profile, doc, ["Node.js"]))
    assert out.method == "template" and any("fact check" in n for n in out.notes)
    good_body = ("Dear Hiring Team,\n\nI would like to apply for the Backend Developer position at Acme. As a "
                 "Fullstack Developer, I designed REST APIs with Node.js, Express and Prisma on PostgreSQL and built "
                 "React and TypeScript dashboards. I also introduced Docker-based local environments and GitHub "
                 "Actions CI/CD. My CV is attached for your review, and I would welcome a conversation.")
    good_ai, _ = fake_ai({"job application e-mail": {"subject": "Application: Backend Developer", "body": good_body}})
    ok = asyncio.run(EmailGenerator(settings, good_ai).ai_email(j, a, profile, doc, ["Node.js"]))
    assert ok.method == "ai" and ok.body.startswith("Dear Hiring Team") and "Kind regards,\nAndi Pratama" in ok.body


# ---------------------------------------------------------------- Gmail SMTP
class FakeSMTP:
    """Stand-in for smtplib.SMTP. `script` is a list of actions, one per connection attempt."""

    instances: list["FakeSMTP"] = []

    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def __call__(self, host: str, port: int, timeout: float = 0, **kw: Any) -> "FakeSMTP":
        action = self.script.pop(0)
        if isinstance(action, OSError):
            raise action
        conn = FakeSMTP(self.script)
        conn.action, conn.calls, conn.host, conn.port = action, [], host, port
        FakeSMTP.instances.append(conn)
        return conn

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")
        assert context is not None and context.check_hostname

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        if self.action == "auth_fail":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg: Any) -> dict:
        import smtplib

        self.calls.append("send")
        a = self.action
        if isinstance(a, tuple):
            raise smtplib.SMTPDataError(a[0], a[1].encode())
        if a == "refused":
            raise smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"5.1.1 user unknown")})
        if a == "drop":
            raise smtplib.SMTPServerDisconnected("connection lost")
        return {}


def _smtp(script: list[Any], attempts: int = 3):
    from src.applications.smtp_client import SmtpClient

    async def no_sleep(_: float) -> None:
        return None

    FakeSMTP.instances = []
    return SmtpClient("me@gmail.com", "abcd efgh ijkl mnop", smtp_factory=FakeSMTP(script), max_attempts=attempts,
                      sleep=no_sleep)


def _msg():
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@gmail.com", "jobs@acme.co.id", "Application"
    m.set_content("hello")
    return m


def test_smtp_success_uses_starttls_and_strips_password_spaces() -> None:
    client = _smtp(["ok"])
    asyncio.run(client.send(_msg()))
    conn = FakeSMTP.instances[0]
    assert (conn.host, conn.port) == ("smtp.gmail.com", 587)
    assert conn.calls[:3] == ["ehlo", "starttls", "ehlo"] and conn.calls[3] == ("login", "me@gmail.com", "abcdefghijklmnop")
    assert "abcd" not in repr(client)
    from src.utils.logger import redact
    assert "abcdefghijklmnop" not in redact("pw=abcdefghijklmnop")


@pytest.mark.parametrize("action,exc,connections", [
    ("auth_fail", GmailAuthError, 1),
    ((550, "5.4.5 Daily user sending limit exceeded"), GmailSendingLimitError, 1),
    ((421, "4.7.0 Try again later, closing connection"), GmailQuotaError, 1),
    ((550, "5.7.1 Our system has detected an unusual rate of unsolicited mail"), GmailPolicyError, 1),
    ("refused", GmailRequestError, 1),
    ("drop", DeliveryUnknownError, 1),
])
def test_smtp_errors_are_not_retried(action: Any, exc: type, connections: int) -> None:
    client = _smtp([action, "ok", "ok"])
    with pytest.raises(exc) as e:
        asyncio.run(client.send(_msg()))
    assert len(FakeSMTP.instances) == connections
    if action == "refused":
        assert "invalid recipient" in str(e.value)


def test_smtp_transient_errors_are_retried() -> None:
    client = _smtp([ConnectionRefusedError("down"), (451, "4.3.0 Mail server temporarily rejected"), "ok"])
    asyncio.run(client.send(_msg()))
    assert len(FakeSMTP.instances) == 2  # connect failure + 451, then success
    client = _smtp([(451, "4.3.0 temp")] * 3)
    with pytest.raises(GmailTransientError):
        asyncio.run(client.send(_msg()))


def test_smtp_requires_full_app_password(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    from src.applications.email_service import transport_kind
    from src.applications.smtp_client import SmtpClient

    with pytest.raises(GmailAuthError, match="GMAIL_APP_PASSWORD"):
        SmtpClient("me@gmail.com", "short")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert transport_kind(settings) == "gmail_api"
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    assert transport_kind(settings) == "smtp"
    settings.raw["applications"]["sending"]["transport"] = "gmail_api"
    assert transport_kind(settings) == "gmail_api"



REAL_NON_APPLICATION = {  # verbatim sentences from live postings (2026-11-23)
    "servicenow": "If you require a reasonable accommodation to complete any part of the application process, or are "
                  "unable to use this online application and need an alternative method to apply, please contact "
                  "globaltalentss@servicenow.com for assistance.",
    "clickup": "If you have questions or need an accommodation in the recruitment process, please contact us at "
               "AskPeople@ClickUp.com. Please disregard any offers received outside these channels and report them "
               "to support@clickup.com.",
    "customerio": "Please be vigilant in all of your job search activity, and if you have any questions please "
                  "contact jobs@customer.io.",
    "arbor": "if you require any reasonable adjustments during your application or interview process, please reach "
             "out to a member of the team at careers@arbor-education.com.",
    "bosch": "If you still need assistance, please feel free to contact us at the following email address - "
             "talentacquisitionhungary@bosch.com - We would be happy to help.",
    "teamworks": "To request a reasonable accommodation, please email talent@teamworks.com.",
    "airbnb": "If you are a candidate with a disability and require reasonable accommodation in order to submit an "
              "application, please contact us at: reasonableaccommodations@airbnb.com .",
}


@pytest.mark.parametrize("name", sorted(REAL_NON_APPLICATION))
def test_real_accommodation_and_help_addresses_are_not_application_inboxes(name: str) -> None:
    assert find_emails(REAL_NON_APPLICATION[name]) == []


@pytest.mark.parametrize("text,expected", [
    ("Kirim CV dan surat lamaran ke hrd@pt-maju.co.id paling lambat 30 Desember.", ["hrd@pt-maju.co.id"]),
    ("To apply, send your resume to careers@startup.io with the subject Backend.", ["careers@startup.io"]),
    ("Acme | Backend Engineer | Remote | Apply: jobs@acme.dev", ["jobs@acme.dev"]),
    ("Contact: recruitment@acme.co.id", ["recruitment@acme.co.id"]),
    ("Send your CV to hr@x.co.id. Questions? hr@x.co.id or info@x.co.id", ["hr@x.co.id"]),
])
def test_application_inboxes_are_found(text: str, expected: list[str]) -> None:
    assert find_emails(text) == expected


def test_recipient_validation_rejects_accommodation_desks() -> None:
    for addr in ("accommodations@coinbase.com", "candidate_accessibility@elastic.co", "askpeople@clickup.com"):
        assert not validate_address(addr)[0]


def test_domain_cap_is_per_address_for_freemail(tracker: ApplicationTracker) -> None:
    from src.applications.sending_limits import recipient_key

    assert recipient_key("hr.alpha@gmail.com") != recipient_key("hr.beta@gmail.com")
    assert recipient_key("x+tag@gmail.com") == recipient_key("x@gmail.com") and recipient_key("a@acme.id") == "@acme.id"
    clock = Clock()
    g = SendingGuard(tracker, _limits(max_per_recipient_domain_per_day=1, max_per_hour=10, max_per_day=10),
                     transport_name="mock", now=clock)
    g.record("1", "sent", "hr.alpha@gmail.com")
    clock.advance(61)
    assert g.check("hr.beta@gmail.com").allowed          # different employer on gmail.com
    assert g.check("hr.alpha@gmail.com").code == "domain_limit"
    g.record("2", "sent", "jobs@acme.id")
    clock.advance(61)
    assert g.check("careers@acme.id").code == "domain_limit"  # same company domain


MODERN_CV = """BUDI SANTOSO
 Fullstack Engineer
 budi@example.org | +62-811-0000-1111 | Website | GitHub

PROFILE

Fullstack Engineer with 3+ years of experience building web applications for
thousands of users.

PROFESSIONAL EXPERIENCE

Developer Supervisor — PT Contoh Asuransi Mar 2025 – Present
▸ Refined claim calculation logic and resolved overpayment bugs (reducing losses
 by 80%), while automating processing for tens of thousands of records.
▸ Led the engineering team building the HR system (React, Laravel, NestJS)
 featuring payroll integration.

Frontend Developer — PT Contoh Teknologi Aug 2023 – Dec 2023
▸ Integrated maps into the WebGIS interface.
▸ Developed robust backend REST APIs for real-time data synchronization between servers and user
 interfaces.
PROFESSIONAL PROJECTS

PRISA — Digital Insurance Platform
▸ Developed policy management and claim modules supporting >300 providers.
▸ Enhanced performance through caching and background jobs.

TECHNICAL SKILLS

Programming Languages JavaScript, TypeScript, PHP, Python
Backend Node.js, Express.js, NestJS, Laravel
Database & Performance PostgreSQL, MySQL, Redis, Data Modeling, Query
 Optimization, Caching
UI/UX Figma (Wireframing, Prototyping), Material Design

EDUCATION

B.Sc. in Information Technology — Universitas Contoh 2017 – 2021 | GPA: 3.90 / 4.00
"""


def test_modern_bulleted_cv_is_structured(settings, tmp_path: Path) -> None:
    from src.cv.models import ParsedCV

    cv = ParsedCV(filename="cv.txt", file_format="txt", text=MODERN_CV, pages=1, sha256="x",
                  links=["mailto:budi@example.org", "https://github.com/budi-example?tab=repos"])
    profile = CVAnalyzer(settings).analyze(cv)
    assert profile.experience_years >= 3.0  # explicit "3+ years" is used when date ranges add up to less
    doc = structure_cv(cv, profile)
    assert doc.identity.name == "Budi Santoso" and doc.identity.links == ["github.com/budi-example"]
    assert [(e.title, e.company) for e in doc.experience] == [("Developer Supervisor", "PT Contoh Asuransi"),
                                                               ("Frontend Developer", "PT Contoh Teknologi")]
    assert len(doc.experience[0].bullets) == 2 and doc.experience[0].bullets[0].endswith("tens of thousands of records.")
    assert doc.experience[1].bullets[1].endswith("between servers and user interfaces.")
    assert [(p.title, p.company, len(p.bullets)) for p in doc.project_entries] == [("PRISA", "Digital Insurance Platform", 2)]
    groups = dict(doc.skill_groups)
    assert groups["Programming Languages"] == ["JavaScript", "TypeScript", "PHP", "Python"]
    assert "Query Optimization" in groups["Database & Performance"]
    assert groups["UI/UX"][0] == "Figma (Wireframing, Prototyping)"
    j = job(job_title="Backend Engineer", required_skills=["PostgreSQL", "NestJS"])
    a = JobAnalyzer(settings).analyze_deterministic(j)
    m = CVMatcher(settings, profile).match(j, a, doc)
    settings.raw["applications"]["generated_cv_directory"] = str(tmp_path)
    tcv, path = asyncio.run(CVGenerator(settings).generate(doc, a, m, company="Acme", title="Backend Engineer", app_id="a1"))
    import pymupdf
    text = pymupdf.open(str(path))[0].get_text()
    assert "Backend:" in text and "Programming Languages:" in text and "PRISA - Digital Insurance Platform" in text
    assert tcv.skill_groups[0][0] in ("Backend", "Database & Performance")  # job-relevant group first
    e = EmailGenerator(settings).template_email(j, profile, doc.identity, doc=doc)
    assert "with 3+ years of experience" in e.body and "Budi Santoso" in e.subject


def test_eligibility_skips_onsite_abroad_and_german_market(settings) -> None:
    f = EligibilityFilter(settings)
    assert not f.evaluate(job(country="United States", work_arrangement="onsite")).eligible
    assert f.evaluate(job(country="United States", work_arrangement="remote")).eligible
    assert f.evaluate(job(country="Indonesia", work_arrangement="onsite")).eligible
    assert not f.evaluate(job(job_title="Frontend Developer (m/w/d)")).eligible
    assert EligibilityFilter(settings, {"allow_onsite_abroad": True}).evaluate(
        job(country="United States", work_arrangement="onsite")).eligible
    g = EligibilityFilter(settings, candidate_seniority="junior")
    assert not g.evaluate(job(seniority="senior")).eligible and g.evaluate(job(seniority="mid")).eligible


def test_alias_skill_counts_as_matched(settings) -> None:
    from src.cv.models import ParsedCV

    cv = ParsedCV(filename="cv.txt", file_format="txt", pages=1, sha256="y",
                  text=MODERN_CV.replace("JavaScript, TypeScript, PHP, Python", "JavaScript, Golang, Python"))
    profile = CVAnalyzer(settings).analyze(cv)
    assert "Go" in profile.skills
    doc = structure_cv(cv, profile)
    j = job(required_skills=["Go"])
    m = CVMatcher(settings, profile).match(j, JobAnalyzer(settings).analyze_deterministic(j), doc)
    assert m.missing_skills == [] and m.matched_skills[0]["skill"] == "Go"
