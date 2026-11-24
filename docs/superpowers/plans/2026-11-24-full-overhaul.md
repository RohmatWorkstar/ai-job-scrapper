# Full Feature & Business-Flow Overhaul (Opsi C) Implementation Plan

> **For agentic workers:** Execute phase-by-phase with strict TDD/regression verification after each pillar. Every pillar must leave the offline pytest suite passing before moving on.

**Goal:** Resolve all Critical/High/Medium audit defects, close the email-only application gap with Web/ATS Apply Assist + full lifecycle tracking, unify duplicated CV & matching cores, upgrade Indonesia/SEA & email discovery with two verified free public sources, integrate application tracking into reports, and ship a single-door Interactive Hub at `python main.py`.

**Architecture:** Six cohesive pillars built incrementally over the existing Python 3.12 async pipeline (`src/core`, `src/cv`, `src/extraction`, `src/intelligence`, `src/discovery`, `src/sources`, `src/storage`, `src/applications`, `src/reporting`, `src/cli`). All existing CLI flags, import paths, and SQLite v1/v2 databases remain backward compatible.

**Tech Stack:** Python 3.12 (`.venv`), SQLite3 (WAL), httpx, BeautifulSoup4, PyMuPDF (`fitz`), python-docx, openpyxl, Rich, Questionary, PyYAML, pytest. No new external runtime dependencies.

---

## Global Constraints
- **Spec:** `docs/superpowers/specs/2026-11-24-full-overhaul-design.md` (approved by user after independent builder-check).
- **Anti-Fabrication:** Never invent skills, dates, employers, numbers, or claims in profiles, tailored CVs, emails, or cover notes.
- **Privacy & Consent:** Never call Gemini when `--no-ai`, `ai.enabled=false`, or declined/unset non-interactive consent applies. Never log raw secrets or break JSON log lines.
- **Safety Gates:** Never send email without `APPROVED` status, valid `approved_hash`, `SendingGuard`, and exclusive `send_lock`. `channel == "web"` records must NEVER be approved for SMTP sending.
- **Backward Compatibility:** Keep all existing CLI flags, `src.applications.cv_document` imports, and v1/v2 → v3 in-place SQLite upgrades intact.

## Review Focus
- Strict enforcement of `send_lock IS NULL` in `tracker.transition()` and atomic `set_content()` / `requeue()`.
- Exact `merge_ai_profile()` anti-fabrication logic and `JsonFormatter` recursive redaction.
- Orthogonal `mode` (`basic|ai`) vs `channel` (`email|web`) semantics, cross-channel blocking, and `--channel all` shared cap.
- Report `Application Tracker` sheet (global snapshot, omitted when empty) and status precedence.
- Full offline pytest pass (all 198 existing tests + new regression/feature tests) and `graphify update .`.

---

## Phase 1 — Pillar 1: Security, Privacy, Safety & Core Bug Hardening

**Files:**
- Modify: `src/utils/logger.py`, `src/utils/validators.py`, `src/utils/http_client.py`
- Modify: `src/cv/profile_builder.py`, `src/cv/analyzer.py`
- Modify: `src/extraction/content_parser.py`, `src/extraction/html_extractor.py`, `src/extraction/job_normalizer.py`, `src/extraction/browser_extractor.py`
- Modify: `src/intelligence/verification.py`, `src/intelligence/matching.py`, `src/intelligence/relevance.py`
- Modify: `src/core/orchestrator.py`, `src/cli/questions.py`
- Modify: `src/applications/contacts.py`, `src/applications/smtp_client.py`
- Tests: `tests/unit/test_overhaul_pillar1.py` (new) + existing `tests/unit/test_extraction.py`, `tests/unit/test_intelligence.py`, `tests/unit/test_applications.py`

**Concrete Changes:**
1. **`src/utils/logger.py`**: Add recursive `_redact_obj(obj)` that redacts only string leaves (and registers `GMAIL_APP_PASSWORD` stripped variants). Replace `json.loads(redact(json.dumps(...)))`.
2. **`src/utils/validators.py`**: Catch `ValueError` in `validate_url_syntax()` (`urlsplit`) → raise `UnsafeURLError`. Add light mojibake repair in `clean_text()` (`â€™`→`’`, `â€“`→`–`, `â€”`→`—`, `â€œ`/`â€\x9d`→`"`, `Â\xa0`→` `, etc.).
3. **`src/utils/http_client.py`**: Never retry `error_kind in {"content_type", "too_large", "unsafe_url", "blocked_robots", "policy"}`. Default JSON bodies (`application/json`, `application/ld+json`) to `utf-8` when charset header is missing.
4. **`src/cv/profile_builder.py`**: Fix `merge_ai_profile()` skill evidence check (`appears_in(text_lower, name)` OR canonical already detected in `cv_text` OR non-empty verbatim quote containing `name` or canonical detected in `quote`). Guard all `ai.get(...)` list iterations with `isinstance(..., list)`.
5. **`src/extraction/content_parser.py`**:
   - Salary: add `\b` to `_MULT`, guard against funding/revenue/user-count context (±40 chars), enforce currency sanity caps, require salary context (±60 chars) for bare `_JUTA_RE`.
   - Seniority: keep full patterns on `title`; on `description[:1500]` match only explicit role-requirement phrases (`seeking/looking for/hiring/level:/seniority:/entry-level/fresh graduate`), avoiding reporting-line false positives (`VP`, `Director`, `Engineering Manager`, `Lead`).
6. **`src/extraction/html_extractor.py`, `job_normalizer.py`, `browser_extractor.py`**:
   - Keep up to 40,000 chars on `JobPostingLD.description` and `PageExtraction.text` / `enrich_with_page()` so `find_emails()` runs on full text before `JobNormalizer.normalise()` truncates `job_description` to `max_description_chars`.
   - Consume `required_skills` (validated via `SkillDetector.canonical` + `_supported`) and `application_instructions` (running `find_emails()`) in `ai_extract()`.
   - Make `BrowserRenderer._lock` lazy per running loop and call `await BrowserRenderer.shared().aclose()` in `Orchestrator.run()` `finally` when `extraction.use_browser_rendering` is enabled.
7. **`src/intelligence/verification.py`, `matching.py`, `relevance.py`**:
   - Restrict Indonesian `biaya` scam trigger to fee/payment nouns (`biaya\s+(?:pendaftaran|administrasi|pelatihan|tes|test|interview|seragam|materai|jaminan|tiket|akomodasi|deposit)`).
   - Period-normalize `Matcher.salary()` (`hour`×2080, `month`×12 → annual equivalent; infer candidate period from currency/magnitude unless specified).
   - Expand `NON_TECH_TITLE` in `Prefilter` (GTM, client partner, partnerships, business development, operations manager, strategy and operations, revenue operations, field marketing, certification assessment) and check `NON_TECH_TITLE` before `_role_re`/`_skill_re` unless a core software title keyword is present.
8. **`src/core/orchestrator.py`**: Accept `use_ai: bool | None = None` in `run()`, honoring `use_ai=False` with disabled `AIManager`. Run `Verifier` + `Matcher` in exception fallback so emergency reports have real scores/statuses. Close `BrowserRenderer` if used.
9. **`src/cli/questions.py`**: Parse Indonesian thousands separators (`10.000.000` → `10000000.0`), clear `extra_languages` when switching away from `"additional"`.
10. **`src/applications/contacts.py` & `smtp_client.py`**: Fix `select_recipient()` to filter freemail before choosing `best` when `allow_freemail=False` and prefer `company_website` domain > corporate > freemail. Route `SMTPRecipientsRefused` through `classify_smtp()`.

**Gate:** `.venv\Scripts\python.exe -m pytest tests/unit/test_overhaul_pillar1.py tests/unit/test_extraction.py tests/unit/test_intelligence.py tests/unit/test_applications.py -q`

---

## Phase 2 — Pillar 2: Unified CV Pipeline & Aligned Matching/Generators

**Files:**
- Create: `src/cv/document.py`
- Modify: `src/applications/cv_document.py` (re-export facade)
- Modify: `src/cv/parser.py`, `src/cv/analyzer.py`, `src/ai/prompts.py`
- Modify: `src/applications/cv_matcher.py`, `src/applications/cv_generator.py`, `src/applications/email_generator.py`
- Modify: `config/email_templates.yaml`
- Tests: `tests/unit/test_overhaul_pillar2.py` (new)

**Concrete Changes:**
1. Move `CVDocument`, `ExperienceEntry`, `ContactIdentity`, `extract_identity`, `parse_experience`, `parse_skills`, `structure_cv`, `structure_cv_ai` into `src/cv/document.py`; turn `src/applications/cv_document.py` into a clean re-export facade.
2. Fix `_verbatim_norm()` bullet set (`▸►‣⁃◆■➢➤✓✔→`), fix `parse_experience()` continuation guard against headers, include `project_entries` and `experience[].dates` in `ai_safe_dict()`.
3. Upgrade `src/cv/parser.py`: column-aware PDF block extraction, extended bullet normalization, and DOCX hyperlink extraction into `ParsedCV.links`. Align `src/cv/analyzer.py` (`ROLE_WORDS` + bullet cleaning).
4. Enhance `redact_personal(text, extra_names=...)` in `src/ai/prompts.py`; add `cover_note_prompt()`.
5. Upgrade `src/applications/cv_matcher.py` to accept optional `config`/`preferences`, match `doc.project_entries` bullets, and apply negative-preference / arrangement / location-restriction concerns and penalty.
6. Upgrade `src/applications/cv_generator.py` to accept `experience_years`, reorder `skill_groups` on AI `skills_order` with substring/canonical matching, and accept `existing_path` to overwrite cleanly on regeneration.
7. Upgrade `src/applications/email_generator.py` & `config/email_templates.yaml`: pass `dates` & `projects` to AI, honor `email_language` (`en|id|auto`), fix double tone-check false positives on unstripped title/company, relax length floor to 140 chars, deduplicate title repetition in template `role_sentence`, improve ID template achievement support, and add `cover_note()` for `channel="web"`.

**Gate:** `.venv\Scripts\python.exe -m pytest tests/unit/test_overhaul_pillar2.py tests/unit/test_cv_analysis.py tests/unit/test_applications.py -q`

---

## Phase 3 — Pillar 3: Discovery Overhaul & New Free Public Sources

**Files:**
- Modify: `src/sources/communities/hackernews.py`, `src/sources/communities/reddit.py`
- Modify: `src/discovery/query_generator.py`
- Create: `src/sources/job_portals/rss_feeds.py` (`WeWorkRemotelySource`)
- Modify: `src/sources/job_portals/remote_feeds.py` (`WorkingNomadsSource`, remove premature 6k truncation)
- Modify: `src/sources/ats_platforms/ats.py` (remove premature truncation, prioritize SEA boards when `wants_indonesia`)
- Modify: `src/discovery/source_registry.py` (register `WeWorkRemotelySource`, `WorkingNomadsSource`)
- Modify: `config/settings.yaml`, `config/search_profiles.yaml`
- Tests: `tests/unit/test_overhaul_pillar3.py` (new)

**Concrete Changes:**
1. HN monthly window: `effective_window = max(window, 35)` when `window is not None and window >= 7`; record note in `report.notes`; keep untruncated text.
2. Query generator: cycle all `hiring_phrases[lang]`, add explicit email-application queries (`"send your CV" OR "apply via email"` / `"kirim CV" OR "kirim lamaran"`), fix target country rotation across all selected/worldwide countries.
3. Implement `WeWorkRemotelySource` (stdlib `xml.etree.ElementTree`, dynamic category feeds by `role_families`, max 2 feeds/run, 4h cache TTL, full description + `Company: Role` parsing) and `WorkingNomadsSource` (`https://www.workingnomads.com/api/exposed_jobs/`, 4h cache TTL, full description).
4. Add verified SEA/Indonesia ATS seed boards (`flip`, `kargo`, `Wise`, `DeliveryHero`) and prioritize SEA boards first when `config.wants_indonesia` is True.

**Gate:** `.venv\Scripts\python.exe -m pytest tests/unit/test_overhaul_pillar3.py tests/unit/test_sources.py tests/integration/test_pipeline.py -q`

---

## Phase 4 — Pillar 4: Schema v3, Multi-Channel Applications & Tracker

**Files:**
- Modify: `src/storage/database.py`, `src/storage/repository.py`
- Modify: `src/applications/models.py`, `src/applications/tracker.py`, `src/applications/eligibility.py`
- Modify: `src/applications/email_service.py`, `src/applications/setup_check.py`, `src/applications/manager.py`
- Tests: `tests/unit/test_overhaul_pillar4.py` (new) + `tests/unit/test_applications.py` + `tests/integration/test_application_flow.py`

**Concrete Changes:**
1. **Schema v3 (`database.py`)**: Version-aware idempotent migration adding `channel TEXT NOT NULL DEFAULT 'email'`, `closed_by TEXT`, `sender_name TEXT`, `follow_up_at TEXT` plus indexes `ix_app_channel`, `ix_app_recipient`. Bump `SCHEMA_VERSION = 3`.
2. **Repository (`repository.py`)**: Add `recent_opportunities(days=14)` returning deduplicated opportunities across recent sessions.
3. **Models (`models.py`)**: Add `APPLIED_WEB`, `INTERVIEW`, `OFFER`, `EMPLOYER_REJECTED`, `WITHDRAWN`, update `TRANSITIONS` and `BLOCKING_STATUSES` (including `EMPLOYER_REJECTED`, `WITHDRAWN`), update `make_dedupe_key(..., job_url=None)` fallback to `canonical_url(job_url)`, add `channel`, `closed_by`, `sender_name`, `follow_up_at` fields to `ApplicationRecord`.
4. **Tracker (`tracker.py`)**:
   - Enforce `AND send_lock IS NULL` in `transition(..., allow_locked=False)`.
   - Atomic single-transaction `set_content()` and `requeue()`.
   - Normalize Gmail `+tag` in `find_recipient_blocking()`.
   - Forbid `allow_duplicate` in `update_fields()`.
   - Refuse `approve()` on `channel == "web"` (`InvalidTransitionError`).
   - Add `mark_applied_web(app_id, note=...)`, `update_pipeline_status(app_id, status, note=..., follow_up_at=...)`, `latest_status_by_job()`, `global_snapshot(limit=500)`.
   - Order `list([APPROVED])` by `COALESCE(match_score, 0) DESC, created_at ASC`.
5. **Eligibility (`eligibility.py`)**: Support `require_email: bool = True`; when `False`, require `job.application_url or job.source_url`.
6. **EmailService & setup_check (`email_service.py`, `setup_check.py`)**:
   - CRLF `email.policy.SMTP`, optional `reply_to` when `APPLICANT_EMAIL != GMAIL_SENDER_ADDRESS`, `preflight()` validation of subject/body/template placeholders, resolve `sender_name` from `rec.sender_name or os.environ.get("APPLICANT_NAME") or ""`.
   - `TestRedirectTransport`: allow `APPLICATION_TEST_RECIPIENT == GMAIL_SENDER_ADDRESS`; pass `recipient=None` to both `guard.check(None)` and `guard.record(rec.id, "sent", None)`.
   - Validate `recipient_cooldown_days` (`0..3650`) and `max_attachment_mb` (`<= 18`).
7. **Manager (`manager.py`)**:
   - Keep `mode` (`basic|ai`) orthogonal to `channel` (`email|web`).
   - Add `prepare_web(job, mode=..., ...)` and multi-channel `prepare_from_session(..., channel="email|web|all", all_recent_days=None)` with shared combined cap sorted by `relevance_score DESC` for `channel="all"` (email preferred per job, fallback to web).
   - Refuse `auto_approve()` on `channel == "web"`.
   - Fix `_already_closed()` with `closed_by='user'` cross-channel vs `closed_by='system'` scoped to `(job_key, mode, channel, session_id)` and add `previously_closed` counter.

**Gate:** `.venv\Scripts\python.exe -m pytest tests/unit/test_overhaul_pillar4.py tests/unit/test_applications.py tests/integration/test_application_flow.py -q`

---

## Phase 5 — Pillar 5: Reporting & Tracker Integration

**Files:**
- Modify: `src/reporting/report_models.py`, `src/reporting/excel_generator.py`, `src/reporting/docx_generator.py`
- Modify: `src/core/orchestrator.py` (populate `application_statuses` & `applications` in `run()` and `regenerate_reports()`)
- Tests: `tests/unit/test_overhaul_pillar5.py` (new) + `tests/unit/test_storage_and_reports.py`

**Concrete Changes:**
1. Add `application_statuses: dict[str, str]`, `applications: list[dict[str, Any]]`, and `email_applicable` count to `ReportData`.
2. Add `Apply Channel`, `Application Email`, `Application Status` columns next to `Application URL` in `ExcelReportGenerator`, plus `Application Tracker` sheet created only when `d.applications` is non-empty.
3. Show `Email-applicable opportunities` in DOCX executive summary and show `Application e-mail` + `Application status` in `_job_block()`.
4. Wire `ApplicationTracker(self.db)` snapshot loading in `Orchestrator.run()` and `Orchestrator.regenerate_reports()`.

**Gate:** `.venv\Scripts\python.exe -m pytest tests/unit/test_overhaul_pillar5.py tests/unit/test_storage_and_reports.py -q`

---

## Phase 6 — Pillar 6: Interactive Hub, CLI Wiring & Docs/Config Polish

**Files:**
- Create: `src/cli/hub.py` (`InteractiveHub`)
- Modify: `src/cli/apply_cli.py`, `src/cli/application.py`
- Modify: `config/settings.yaml`, `README.md`
- Tests: `tests/unit/test_overhaul_pillar6.py` (new)

**Concrete Changes:**
1. **`src/cli/apply_cli.py`**:
   - Support `--apply basic|ai|web`, `--channel email|web|all` (error on `--apply web --channel email`), `--all-recent [DAYS]`, `--update-status <APP_ID> <STATUS> [--note]`, `--status <STATUS>` filter on `--applications` (with Channel column + pipeline funnel).
   - Web/ATS Assist review menu (Open URL, Open CV PDF, Open CV folder, Edit cover note, Regenerate CV with live AI, Mark as Applied `APPLIED_WEB`, Decide later, Reject `closed_by='user'`, Skip `closed_by='user'`, Stop).
   - Fix `--review` (call `validate_applications_settings()`, load matching `rec.original_cv_path`, init `AIManager` with consent), `--send-queue` (`sender_name` per record), `_profile()` AI upgrade, confirmation wording, no-op edit detection, `shlex.split($EDITOR)`.
2. **`src/cli/hub.py`**: Implement `InteractiveHub` with live status strip (latest session, funnel counts, sending badge) and 8 menu entries (Search Jobs + prepare prompt, Browse & Filter Jobs + instant prepare, Prepare Applications, Review Pending, Send Queue, Application Pipeline & Stage Update, Setup/Health/Safety submenu, Exit).
3. **`src/cli/application.py`**: Add `--search`, `--ai-consent`, `--channel`, `--all-recent`, `--update-status`, `--note`, `--status`, extend `--apply` to `["basic", "ai", "web"]`. Strictly propagate `use_ai` into `questionnaire()` and `run_pipeline()` → `Orchestrator.run(..., use_ai=use_ai)`. Launch `InteractiveHub` on interactive flag-free runs while keeping `--defaults` / non-interactive / all explicit flags 100% backward compatible. Replace redundant `Orchestrator` instantiations with `Database`/`Repository`.

**Gate:** `.venv\Scripts\python.exe -m pytest -v` (full suite) + `graphify update .` + `git status` / `git diff --stat`.
