# Design Spec: Full Feature & Business-Flow Overhaul (Opsi C)

Date: 2026-11-24 · Branch: `feat/email-application-automation` · Base commit: `65e166e`

---

## 1. Intent & Why

An end-to-end audit of `Job-Scrapper` (codebase + empirical inspection of the 319 scraped opportunities in `data/database/job_intelligence.sqlite3`) exposed two classes of problems:

1. **Broken Business Flow:** The scraping pipeline and the application automation module (`src/applications/`) are structurally disconnected. Every single scraped posting in the real local database (`319/319`) applies via a web/ATS URL rather than e-mail, locking 100% of jobs out of tailored CV generation, cover-note drafting, and application tracking. Meanwhile, the strongest free source of e-mail postings (Hacker News *"Who is hiring"*) drops `95/95` posts for 23 days each month due to the 7-day publication window, e-mail hiring phrases in `config/search_profiles.yaml` are unreachable dead config, Indonesia is absent from the top 10 discovered countries, reports omit application e-mails and tracking status, and daily usage requires memorizing 15+ CLI flags.
2. **Critical Technical, Privacy & Integrity Defects:** `--no-ai` and declined AI consent still invoke Gemini inside `Orchestrator`; `merge_ai_profile()` has a broken boolean expression that lets Gemini inject fabricated taxonomy skills; `JsonFormatter` crashes with `JSONDecodeError` on 10+ digit numbers or unrounded floats; `ApplicationTracker.transition()` lacks a `send_lock IS NULL` guard (multi-process race condition); salary parsing turns funding amounts and `"$7900/mo"` into `$250M`–`$7.9B`; seniority parsing misclassifies 22.6% of regular roles as `lead`; Indonesian `"biaya"` benefits false-trigger `POSSIBLE_SCAM`; and `--review` / `--send-queue` drop AI capability and applicant identity metadata.

The goal of this overhaul is to fix every identified defect, close the business-flow chasm with **multi-channel application support (Email + Web/ATS Assist)**, unify duplicated CV and matching engines into a single source of truth, expand free public discovery for Indonesia/SEA and remote engineering roles, integrate tracking into reports, and provide a **single-door Interactive Hub** at `python main.py` without breaking any existing non-interactive CLI or Task Scheduler workflow.

---

## 2. Goals, Non-Goals & Anti-Goals

### Goals
- **G1 (Zero Critical/High Bugs):** Eliminate all Critical (C1–C4), High (H1–H7), and Medium (M1–M5) defects found in the audit with dedicated regression tests.
- **G2 (Multi-Channel Applications — Email + Web/ATS Assist):** Enable `--apply` and the Review UI to prepare tailored ATS PDF CVs, tailored cover notes, and tracked applications for **both** e-mail jobs (`channel="email"`) and web/ATS URL jobs (`channel="web"`).
- **G3 (Full Application Lifecycle Tracking):** Extend `ApplicationTracker` beyond `SENT` to cover `APPLIED_WEB`, `INTERVIEW`, `OFFER`, `EMPLOYER_REJECTED`, and `WITHDRAWN`, with separate tracking of system auto-rejections (`closed_by="system"`) vs manual user decisions (`closed_by="user"`).
- **G4 (Unified Domain Core):** Consolidate CV parsing/structuring into one shared module (`src/cv/document.py`) and align `Matcher` and `CVMatcher` so skill evidence, seniority, and user preferences (`additional_preferences`, work arrangement, location) behave consistently across search and application stages.
- **G5 (Indonesia/SEA & E-mail Discovery Upgrade):** Fix HN monthly windowing, activate e-mail-targeted search queries, fix country rotation, add verified free public sources (`WeWorkRemotelySource`, `WorkingNomadsSource`) and verified SEA/Indonesia ATS seed boards, and integrate `Application Channel`, `Application Email`, `Application Status`, and an `Application Tracker` sheet into reports.
- **G6 (Single-Door Interactive Hub):** Running `python main.py` interactively with no action flags opens a clean Rich + Questionary Hub covering Search, Browse/Filter Jobs, Prepare Applications, Review Pending, Send Queue, Application Pipeline, and Setup/Health. All existing flags remain 100% backward compatible.

### Non-Goals
- Automated submission / form-filling of external web ATS forms (Greenhouse, Lever, LinkedIn, JobStreet, Glints) using headless browsers. Web/ATS Assist prepares the tailored CV + cover note, opens the URL for the user, and records status when the user confirms submission.
- Scraping behind login walls, bypassing CAPTCHAs, or ignoring `robots.txt` / `no_fetch_domains`.
- Adding a web server, frontend framework, or heavy third-party dependencies (stay strictly within existing Python stdlib + `requirements.txt` packages).
- Adding `gmail.readonly` scope or inbox bounce polling (keep least-privilege `gmail.send` / SMTP App Password).

### Anti-Goals (Failure Conditions Even If Code Runs)
- **AG1:** Never fabricate skills, dates, employers, numbers, or claims in profiles, tailored CVs, or e-mails/cover notes.
- **AG2:** Never send data to Gemini when `--no-ai` is passed, `ai.enabled` is `false`, or the user has not explicitly consented.
- **AG3:** Never send an e-mail without explicit `APPROVED` status, matching `approved_hash`, passing `SendingGuard`, and exclusive `send_lock`.
- **AG4:** Never silently drop or lose existing SQLite data during v2 → v3 schema upgrade, and never break existing Task Scheduler commands (`python main.py --send-queue --defaults`, `python main.py --defaults`, etc.).

---

## 3. Global Constraints

- **Python:** 3.11+ (tested on 3.12 in `.venv`).
- **Dependencies:** No new external runtime packages required (`xml.etree.ElementTree` stdlib handles RSS; `shlex`, `sqlite3`, `pymupdf`, `python-docx`, `openpyxl`, `httpx`, `rich`, `questionary`, `PyYAML` are already installed).
- **Platform:** Windows 10/11 primary (UTF-8 console, `os.startfile`, Notepad fallback, Task Scheduler compatibility), cross-platform safe on Linux/macOS (`shlex.split` for `$EDITOR`, `open`/`xdg-open`).
- **Backward Compatibility:**
  - All existing CLI flags (`--cv`, `--defaults`, `--no-ai`, `--max-queries`, `--check`, `--history`, `--report`, `--apply basic|ai`, `--session`, `--limit`, `--review`, `--send-queue`, `--dry-run`, `--auto`, `--allow-duplicate`, `--setup-check`, `--gmail-auth`, `--applications`, `--sending-status`, `--emergency-stop`, `--resume-sending`, `--requeue`) keep working.
  - Existing import paths (especially `src.applications.cv_document`) remain valid via facade re-exports.
  - Existing v1/v2 SQLite databases upgrade cleanly in place to v3 without data loss.
- **Verification Gate:** Entire offline pytest suite (all existing 198+ tests plus comprehensive new tests for every overhaul requirement) must pass cleanly; `graphify update .` must be run after code edits.

---

## 4. Approaches Considered & Selected Approach

| Approach | Scope | Trade-offs | Decision |
|---|---|---|---|
| **Opsi A — Rombak Kecil** | Fix Critical/High/Medium bugs only; keep CLI & email-only application model unchanged. | Lowest risk, fast; but leaves 100% of web/ATS jobs unable to use tailored CVs or tracking, and leaves fragmented 15-flag UX. | Rejected |
| **Opsi B — Rombak Menengah** | Opsi A + Web/ATS Apply Assist, Indonesia/email discovery fixes, report integration, post-apply tracking. | Fixes main business gaps cleanly; still leaves duplicated CV parser + matcher architectures and flag-heavy UX. | Rejected |
| **Opsi C — Rombak Besar** | Opsi B + Interactive Hub menu tunggal (`python main.py`), unified CV parsing & aligned matching domain core, and new verified free public sources (`WeWorkRemotelySource`, `WorkingNomadsSource`, SEA ATS boards). | Highest product quality and long-term maintainability; broader touch surface managed via modular, independently testable phases with full regression coverage. | **Selected by user** |

---

## 5. Architecture & Detailed Design

The overhaul is organized into **6 cohesive pillars**, designed to be built and tested incrementally so each stage leaves the repository green.

### Pillar 1 — Security, Privacy, Safety & Core Bug Hardening

#### 1.1 Strict AI Consent & `--no-ai` Enforcement
- **`src/cli/application.py`:**
  - Add `--ai-consent` boolean flag (explicit opt-in for non-interactive `--defaults` runs).
  - In `ai_consent()`: when `not self.interactive`, return `True` **only** if `self.args.ai_consent` is set OR `self.saved.get("ai_cv_consent") is True` (never default to `True` when unset).
  - Pass `use_ai: bool` explicitly into `questionnaire()` (for accurate `credentials_summary`) and `run_pipeline(profile, config, profile_id, use_ai=use_ai)` → `Orchestrator.run(..., use_ai=use_ai)`.
  - In `Orchestrator.run(..., use_ai: bool | None = None)`: if `use_ai is False`, construct `AIManager(None, disabled_reason="AI disabled for this run (--no-ai or consent declined)")` instead of `AIManager.from_settings()`. When `use_ai is None` (direct API/test callers), preserve existing `AIManager.from_settings()` behavior.
  - Replace redundant `Orchestrator` instantiations in `environment()`, `main_flow()`, `show_history()` with direct `Database` / `Repository` calls.

#### 1.2 Hardened Anti-Fabrication & Safe JSON Logging
- **`src/cv/profile_builder.py`:**
  - Fix `merge_ai_profile()` line 70–71: a skill returned by AI is `supported` **only if** `appears_in(text_lower, name)` OR (`detector.canonical(name)` is already in `detector.detect_names(cv_text)`) OR (`quote` is non-empty AND `appears_in(text_lower, quote[:80])` AND (`appears_in(quote.lower(), name)` OR `canonical.lower() in {s.lower() for s in detector.detect_names(quote)}`)). Never treat bare `detector.canonical(name)` as evidence!
  - Guard all `ai.get(key)` list fields with `isinstance(val, list)` so a string value from Gemini can never be iterated character-by-character.
- **`src/utils/logger.py`:**
  - Replace `json.loads(redact(json.dumps(extra)))` with a recursive `_redact_obj(obj)` helper that traverses `dict`/`list`/`tuple` and applies `redact()` **only to `str` leaves** (converting non-JSON-primitive objects via `str()` first). Numbers (`int`, `float`), booleans, and `None` are NEVER passed through `_PHONE_RE`.
  - Register `GMAIL_APP_PASSWORD` (both raw and all-whitespace-stripped via `re.sub(r"\s+", "", pw)`) and any OAuth access/refresh tokens loaded at runtime.

#### 1.3 Multi-Process `send_lock` Integrity, MIME & Transport Hardening
- **`src/applications/tracker.py`:**
  - In `transition(..., allow_locked: bool = False)`: add `AND send_lock IS NULL` to the SQL `WHERE` clause whenever `allow_locked=False`. Only `mark_sent()`, `mark_rehearsed()`, `mark_failed()`, and explicit `requeue()` pass `allow_locked=True`.
  - Make `set_content()` and `requeue()` execute their state + field updates inside a **single SQLite transaction** (atomic).
  - Normalize recipient emails in `find_recipient_blocking()` with the same Gmail `+tag` stripping rule used in `sending_limits.recipient_key()` (`_norm_recipient_addr()`), so `hr+jobs@gmail.com` and `hr@gmail.com` cannot bypass the 30-day one-application-per-recipient rule.
  - Add `allow_duplicate` to `update_fields()`'s `forbidden` set so post-approval tampering cannot bypass duplicate checks.
- **`src/applications/smtp_client.py`:**
  - In `SMTPRecipientsRefused` handler: inspect every `(code, msg)` in `exc.recipients.values()` and pass through `classify_smtp(code, decoded_msg)` so `550 5.4.5` (daily sending limit) raises `GmailSendingLimitError`, `4xx 4.7.x` raises `GmailQuotaError`, and `5xx 5.7.x` raises `GmailPolicyError` instead of misclassifying them as `invalid_recipient`.
- **`src/applications/email_service.py` & `setup_check.py`:**
  - Use `email.policy.SMTP` (or `msg.as_bytes(policy=email.policy.SMTP)`) when serializing RFC 5322 messages for Gmail API so line endings are strictly CRLF (`\r\n`).
  - Wire optional `reply_to` in `EmailService` (set when `APPLICANT_EMAIL` is configured and differs from `GMAIL_SENDER_ADDRESS`).
  - Strengthen `EmailService.preflight()`: reject empty subject (`len(subject.strip()) == 0`), too-short body (`len(body.strip()) < 30`), or leftover template placeholders (`[Name]`, `{company}`, etc.) with `SendRefused(..., code="invalid_message")`.
  - In `TestRedirectTransport`: allow `APPLICATION_TEST_RECIPIENT` to equal `GMAIL_SENDER_ADDRESS` (so single-Gmail users can test-send to themselves as documented in README §C). When `isinstance(self.transport, TestRedirectTransport)`, `EmailService.send_application()` passes `recipient=None` to **both** `self.guard.check(None)` and `self.guard.record(rec.id, "sent", None)`. Daily, hourly, interval, window, pause, and emergency-stop limits remain enforced on real Gmail sends, while `max_per_recipient_domain_per_day` neither blocks test sends nor pollutes real employer domains.
  - Validate `applications.recipient_cooldown_days` (`0..3650`) in `validate_applications_settings()`, and cap `max_attachment_mb` at `18` MB (accounting for ~33% MIME base64 overhead against Gmail SMTP's 25 MB limit).
- **`src/applications/contacts.py`:**
  - In `select_recipient()`:
    1. Filter out invalid addresses (`validate_address`).
    2. If `allow_freemail=False`, filter out `FREEMAIL_DOMAINS` from `valid` **before** selecting `best` (so corporate addresses are never discarded because a freemail appeared earlier in the list).
    3. Rank valid candidates: (a) address matching `company_website` domain, (b) non-freemail corporate domain, (c) freemail address.
- **`src/utils/validators.py` & `src/utils/http_client.py`:**
  - Wrap `urlsplit()` in `validate_url_syntax()` with `try/except ValueError` raising `UnsafeURLError`.
  - Fix retry condition in `SafeHttpClient._request()`: never retry `error_kind in {"content_type", "too_large", "unsafe_url", "blocked_robots", "policy"}`.
  - Fix mojibake / UTF-8 decoding in `SafeHttpClient._read_body()`: for JSON responses (`application/json`), always default to `"utf-8"` regardless of missing HTTP charset header, and add light repair for common UTF-8-as-Latin-1 mojibake (`â€™` → `’`, `Â\xa0` → ` `) in `clean_text()` / `html_to_text()`.

#### 1.4 Extraction, Verification & Matching Accuracy Fixes
- **`src/extraction/content_parser.py`:**
  - **Salary parser (`parse_salary`):**
    - Add word boundary `\b` to `_MULT` (e.g., `(?:k|K|rb|ribu|jt|juta|m|M|mn|mio|million|billion|miliar)\b`) so `"$7900/mo"` / `"$7900 monthly"` never matches `m` as million!
    - Reject matches preceded/followed (within 40 chars) by non-salary financial context (`series [a-f]`, `funding`, `raised`, `valuation`, `revenue`, `arr`, `gm[vw]`, `market cap`, `users`, `pengguna`, `customers`, `transaksi`, `transactions`, `downloads`, `invested`, `aum`, `assets`).
    - Enforce sanity bounds per currency/period (e.g., reject USD > `2_000_000`, EUR/GBP/SGD/AUD/CAD > `2_000_000`, IDR > `2_000_000_000`).
    - Require salary context (`gaji|salary|upah|kompensasi|penghasilan|take home|thp|remunerasi|rp|idr|/bulan|/bln|per bulan`) within 60 chars for bare `_JUTA_RE` (so `"5 juta pengguna"` is never treated as salary).
  - **Seniority parser (`detect_seniority`):**
    - Keep full `_SENIORITY_PATTERNS` for `title`.
    - For `description[:1500]`, ONLY match explicit **role-requirement** contexts (e.g., `(?:seeking|looking for|hiring|role is|level:|seniority:)\s+(?:a\s+)?(junior|mid[- ]level|senior|staff|principal|lead)` or `entry[- ]level|fresh graduate`), NEVER bare mentions of `"VP"`, `"Director"`, `"Engineering Manager"`, `"Chief"`, `"Lead"`, `"Staff"`, `"Architect"` that appear in reporting lines or company bios!
  - **Title & Role words (`src/cv/analyzer.py`):** add `supervisor|qa|scrum\s+master|product\s+owner|tech\s+lead` to `ROLE_WORDS`.
- **`src/intelligence/verification.py`:**
  - Fix Indonesian scam rule (`verification.py:50`): replace bare `biaya` with `biaya\s+(?:pendaftaran|administrasi|pelatihan|tes|test|interview|seragam|materai|jaminan|tiket|akomodasi|deposit)` so legitimate benefits (*"tunjangan biaya kesehatan"*, *"biaya transportasi ditanggung"*) are never marked `POSSIBLE_SCAM`.
- **`src/intelligence/matching.py` & `src/cli/questions.py`:**
  - Normalize salary periods in `Matcher.salary()` (`hour` × 2080 ≈ `year`, `month` × 12 ≈ `year`; assume IDR `min_salary` is monthly if `< 100_000_000` and USD/EUR/GBP/SGD/AUD/CAD `min_salary` is yearly if `>= 20_000` else monthly, unless user specifies period — or ask optional period when entering `min_salary` in `questions.py`!).
  - Fix `questions.py` salary input parser to handle Indonesian thousands separator (`10.000.000` → `10000000.0`) as well as commas (`10,000,000` → `10000000.0`), and fix `extra_languages` persistence when switching back from `"additional"` to `"both"`.
- **Full-Text Email Extraction Before Truncation:**
  - In `ats.py` (`GreenhouseSource`, `SmartRecruitersSource`), `remote_feeds.py`, `hackernews.py`, `reddit.py`, and `html_extractor.py` (`parse_job_posting_ld`, `extract_from_html`): do NOT truncate `text`/`description` to 6,000–8,000 chars before `JobNormalizer.normalise()` calls `find_emails()`. Extract `contact_emails` from full untruncated text (or keep up to 40,000 chars on `RawDiscovery.text` and let `JobNormalizer.normalise()` truncate `JobOpportunity.job_description` to `max_description_chars` AFTER `find_emails()` runs).
- **Resource & Pipeline Hygiene:**
  - Call `await BrowserRenderer.shared().aclose()` in `Orchestrator.run()` `finally` block (when `use_browser_rendering` was used) and lazily bind `self._lock` per running loop.
  - In `ai_extract()` (`src/extraction/job_normalizer.py`): actually consume and validate `required_skills` (via `SkillDetector.canonical` + `_supported`) and `application_instructions` (run `find_emails()` on it too!).
  - In `Prefilter` (`src/intelligence/relevance.py`): expand `NON_TECH_TITLE` with `gtm|go-to-market|client\s+partner|partnerships|business\s+development|operations\s+manager|strategy\s+and\s+operations|certification\s+assessment|field\s+marketing|revenue\s+operations` and check `NON_TECH_TITLE` **before** `_role_re` / `_skill_re` (unless a strong software title word like `software|backend|frontend|fullstack|devops|sre|data engineer|ml engineer` is also present).
  - In `Orchestrator.run()` exception fallback (`orchestrator.py:297-306`): run deterministic `Verifier` and `Matcher` on whatever partial `unique` jobs were collected so emergency reports still have real scores and verification statuses.

---

### Pillar 2 — Unified Domain Core (Single CV Pipeline & Aligned Matching)

#### 2.1 Single CV Structuring Pipeline
- **New canonical module: `src/cv/document.py`** (moves & upgrades `CVDocument`, `ExperienceEntry`, `extract_identity`, `parse_experience`, `parse_skills`, `structure_cv`, `structure_cv_ai`).
  - `src/applications/cv_document.py` re-exports all public symbols from `src.cv.document` for 100% backward compatibility.
- **Improved `src/cv/parser.py`:**
  - Normalize extended bullet glyphs (`▸►‣⁃◆■➢➤✓✔→●`) consistently.
  - Improve PDF extraction: detect multi-column layouts by inspecting PyMuPDF text blocks (`page.get_text("blocks")`) — when two clear vertical column bands exist with non-overlapping x-ranges (left column `x1 < page_width * 0.48`, right column `x0 > page_width * 0.45` with >= 3 blocks each), read left column top-to-bottom then right column top-to-bottom instead of interleaving via `sort=True`; otherwise read blocks in natural reading order (`sort=False` / block-sorted) without injecting artificial leading margin spaces!
  - Extract DOCX hyperlink targets (`rel.target_ref` for hyperlink relationships in `document.part.rels`) into `ParsedCV.links`, bringing DOCX to parity with PDF.
- **Improved `src/cv/document.py`:**
  - Fix `_verbatim_norm()` to strip the exact same full `_BULLET` character set (`•\-\*–·▪●◦»▸►‣⁃◆■➢➤✓✔→`).
  - Fix `parse_experience()` continuation heuristic so a line matching a header pattern (contains `_RANGE_RE` or `ROLE_WORDS` + separator) is never swallowed as a bullet continuation just because of a leading space.
  - Include `project_entries` (with bullets) and `experience[].dates` in `CVDocument.ai_safe_dict()`.
  - Enhance `redact_personal(text, *, extra_names: list[str] | None = None)` in `src/ai/prompts.py` to also strip the candidate's detected full name and personal website domains before sending CV text to Gemini.
  - `CVAnalyzer.analyze(cv)` reuses the same bullet-cleaning and section parsing so `CandidateProfile` and `CVDocument` never diverge on `projects`, `skills`, or `experience`.

#### 2.2 Aligned Matching, Tailored CV & Email Generation
- **`src/applications/cv_matcher.py`:**
  - Accept optional `config: SearchConfig | None = None` and `preferences: Preferences | None = None`.
  - Include `doc.project_entries` bullets (not just `doc.projects` titles) when computing `relevant_projects` and `recommended_sections`.
  - Evaluate `preferences.negative` (e.g., `"Avoid banking"`) and `work_arrangement` / `location_restrictions` against candidate home country, surfacing explicit warnings in `MatchReport.concerns` and applying a transparent penalty when a negative preference is hit.
- **`src/applications/cv_generator.py`:**
  - Accept `experience_years: float | None = None` (from `CandidateProfile.experience_years`) in `tailor()` / `generate()` so summary fact-checking allows the computed/explicit years even when not literally printed as `"N years"` in the CV.
  - Reorder `tcv.skill_groups` (both group order and within-group item order) when AI supplies `skills_order`, matching canonical/substring names gracefully (`"React"` matches `"React (Next.js)"`).
  - Accept optional `existing_path: Path | None = None` in `generate()` so `regenerate_cv()` overwrites the validated target PDF in place instead of leaving orphan PDFs on disk.
- **`src/applications/email_generator.py` & `config/email_templates.yaml`:**
  - In `ai_email()`:
    - Include `dates` on each experience item AND `projects` (from `doc.project_entries` / `doc.projects`) in `cand` payload so Gemini actually receives the data `application_email_prompt()` asks it to use.
    - Respect `self.default_language` (`applications.email_language: en|id`) unless it is `"auto"` (only use `analysis.language` when `email_language == "auto"`).
    - Do NOT call `check_tone(body, ...)` separately on raw unstripped `body` — `check_free_text(checked, ...)` already checks tone on `checked` (where `job_title` and `company_name` have been stripped), eliminating false rejections on companies/titles like `"Dynamic"` or `"Seamless"`.
    - Relax body length check to `140 <= len(body) <= 3000`.
  - In `template_email()`:
    - Avoid repeating the same title in `role_sentence` when `entry.title.lower() == headline.lower()` (e.g., `"I'm currently a Fullstack Engineer at Acme with 3+ years of experience."` instead of repeating `"as a Fullstack Engineer"` twice).
    - Support `{achievement_sentence}` in Indonesian template too (picking a matching Indonesian or clean English CV bullet prefixed naturally as `"Di peran terakhir saya di {company}, saya ..."` when the bullet starts with an Indonesian verb or omitting cleanly if language mismatch would read awkwardly; keep English bullet quoting verbatim for EN and clean factual skill/project highlight for ID).
    - Add `cover_note(job, analysis, profile, doc, matched_skills)` method for `channel="web"` that produces a concise, paste-ready cover note / application message (without e-mail `Subject:` requirement if pasted in a form, though still storing a descriptive title/subject).

---

### Pillar 3 — Discovery Overhaul (Indonesia/SEA, Email Jobs & Free Public Sources)

1. **Hacker News Monthly-Aware Window (`src/sources/communities/hackernews.py`):**
   - Since `"Ask HN: Who is hiring?"` posts once on the 1st of each month, compute effective window as `max(ctx.config.publication_window_days, 35)` whenever `publication_window_days is not None and publication_window_days >= 7` (if user explicitly asks for 1 or 3 days, respect it). Record a note in `report.notes` explaining the monthly thread window.
2. **Query Generator Upgrade (`src/discovery/query_generator.py`):**
   - Fix dead `hiring_phrases` indexing: cycle across ALL `hiring_phrases[lang]` across `(site, role)` pairs instead of only indices `0..2`.
   - Add an explicit **e-mail application query slice** in `_general()` and `_social()` (when `languages` includes `"en"` or `"id"`): e.g., `"{role}" ("send your CV" OR "apply via email") {where}` and `lowongan "{role}" ("kirim CV" OR "kirim lamaran") email` so Brave Search actively surfaces e-mail-applicable postings.
   - Fix target country rotation in `_social()` and `_ats()`: cycle across `targets` by global counter `idx` rather than `i % len(targets)` (where `i < 3`), covering all selected/worldwide countries.
3. **New Verified Free Public Sources (No API Key Needed):**
   - **`WeWorkRemotelySource`** (`src/sources/job_portals/rss_feeds.py`): Fetches official WWR RSS feeds (`remote-programming-jobs.rss`, `remote-full-stack-programming-jobs.rss`, `remote-back-end-programming-jobs.rss`, `remote-front-end-programming-jobs.rss`, `remote-devops-sysadmin-jobs.rss` — selected dynamically by candidate `role_families`, max 2 feeds/run, 4h cache TTL), parses `title` (`Company: Role`), `region`, `pubDate`, `link`, and full untruncated `description` (also capturing any application e-mails!).
   - **`WorkingNomadsSource`** (`src/sources/job_portals/remote_feeds.py`): Fetches `https://www.workingnomads.com/api/exposed_jobs/` (4h cache TTL), maps `company_name`, `title`, `location`, `pub_date`, `tags`, full `description`, and `url`.
   - Register both in `config/settings.yaml` under `discovery.connectors` and `performance.host_intervals` / `cache_ttl_hours`.
4. **Expanded Verified SEA / Indonesia ATS Seed Boards (`config/search_profiles.yaml`):**
   - Add live-verified SEA/Indonesia boards discovered during recon: `greenhouse: [..., flip, kargo]`, `ashby: [..., flip]`, `smartrecruiters: [..., Wise, DeliveryHero]`, and prioritize SEA/Indonesia boards first in `AtsSource.boards()` when `config.wants_indonesia` is `True`!

---

### Pillar 4 — Multi-Channel Application Automation & Full Pipeline Tracking

#### 4.1 Database Schema v3 (`src/storage/database.py`)
- Implement version-aware, idempotent migration in `Database.migrate()`:
  - Read existing `schema_version` from `meta` (default `0`).
  - Run `SCHEMA` and `APPLICATION_SCHEMA` (`CREATE TABLE IF NOT EXISTS`).
  - If existing `applications` table lacks v3 columns, inspect `PRAGMA table_info(applications)` and add missing columns safely via `ALTER TABLE applications ADD COLUMN ...`:
    - `channel TEXT NOT NULL DEFAULT 'email'` (`'email'` | `'web'`)
    - `closed_by TEXT` (`'system'` | `'user'` | `NULL`)
    - `sender_name TEXT` (stores exact display name approved for sending so `--send-queue` matches review 100%)
    - `follow_up_at TEXT` (optional ISO date for follow-up tracking)
  - Add indexes: `ix_app_channel`, `ix_app_recipient` (`lower(recipient_email)`).
  - Use `isolation_level=None` or `BEGIN IMMEDIATE` helper for atomic write transactions in `ApplicationTracker` to avoid WAL write-upgrade `SQLITE_BUSY` deadlocks under concurrent `--send-queue` + `--review`.
  - Bump `SCHEMA_VERSION = 3`.

#### 4.2 Extended Status Machine (`src/applications/models.py`)
- Add statuses to `ApplicationStatus`:
  - `APPLIED_WEB = "APPLIED_WEB"` (user confirmed manual submission on web/ATS form)
  - `INTERVIEW = "INTERVIEW"`
  - `OFFER = "OFFER"`
  - `EMPLOYER_REJECTED = "EMPLOYER_REJECTED"`
  - `WITHDRAWN = "WITHDRAWN"`
- Updated `TRANSITIONS`:
  - `PENDING_APPROVAL` → `{PENDING_APPROVAL, APPROVED, APPLIED_WEB, REJECTED, SKIPPED}` (`APPLIED_WEB` only valid when `channel == "web"`)
  - `APPROVED` → `{SENT, FAILED, PENDING_APPROVAL, SKIPPED}`
  - `SENT` → `{INTERVIEW, OFFER, EMPLOYER_REJECTED, WITHDRAWN}`
  - `APPLIED_WEB` → `{INTERVIEW, OFFER, EMPLOYER_REJECTED, WITHDRAWN}`
  - `INTERVIEW` → `{OFFER, EMPLOYER_REJECTED, WITHDRAWN}`
  - `OFFER` → `{WITHDRAWN}`
  - `FAILED` → `{PENDING_APPROVAL, SKIPPED}`
  - `REJECTED`, `SKIPPED`, `EMPLOYER_REJECTED`, `WITHDRAWN` → terminal (`set()`).
- Updated `BLOCKING_STATUSES`:
  - `{NEW, ANALYZED, MATCHED, PENDING_APPROVAL, APPROVED, SENT, APPLIED_WEB, INTERVIEW, OFFER, EMPLOYER_REJECTED, WITHDRAWN}` — including `EMPLOYER_REJECTED` and `WITHDRAWN` ensures subsequent runs never auto-recreate applications for terminal employer rejections or withdrawals unless `--allow-duplicate` is explicitly confirmed.
- **Deduplication, Cross-Channel Blocking & Web Queue Guard:**
  - `make_dedupe_key(company, title, recipient=None, job_url=None)` falls back to `canonical_url(job_url)` whenever both `company` and `recipient` are empty, preventing `"?|<title>"` key collisions across web jobs without company names.
  - `find_blocking()` and `closed_by = 'user'` checks apply **across channels**: an active, sent, applied, terminal-outcome, or user-rejected/skipped record on `email` blocks preparing the same job on `web` and vice versa (unless `--allow-duplicate` is explicitly confirmed).
  - `tracker.approve()` and `manager.auto_approve()` explicitly refuse `channel == "web"` by raising `InvalidTransitionError`. Web records never enter the e-mail send queue; they transition `PENDING_APPROVAL → APPLIED_WEB` exclusively via `tracker.mark_applied_web(app_id)` (which also records `sent_at`).
- **Fix `_already_closed()` Permanence Bug (`src/applications/manager.py`):**
  - System eligibility rejections still record a `REJECTED` row with `closed_by = 'system'` for auditability (preserving existing tests that assert rejected records exist in DB).
  - `_already_closed(job, mode=mode, channel=channel, session_id=session_id)` returns `True` ONLY when:
    1. a `REJECTED`/`SKIPPED` record exists for `job_key = job.job_id` with `closed_by = 'user'` (any mode or channel — user explicitly rejected or skipped it), OR
    2. a `REJECTED`/`SKIPPED` record exists with `closed_by = 'system'` matching the exact tuple `(job_key, mode, channel, session_id)` (prevents duplicate system-rejection rows when re-running the exact same command on the same session, while allowing `--apply ai` or `--apply web` to evaluate jobs auto-rejected by `--apply basic`, and allowing new search sessions to re-evaluate cleanly).
  - In `_prepare_ai()` and `prepare_web()`: report `_already_closed` jobs in a dedicated `summary.previously_closed` counter instead of misreporting them under `invalid_email`.

#### 4.3 Multi-Channel Application Flow (Email + Web/ATS Assist)
- **Orthogonal `mode` and `channel`:**
  - `ApplicationRecord.mode` remains strictly `"basic"` | `"ai"`, and `ApplicationRecord.channel` is strictly orthogonal (`"email"` | `"web"`).
  - Concrete CLI mapping (clean, intuitive, 100% backward compatible):
    - `python main.py --apply basic` → `mode="basic", channel="email"` (unchanged default).
    - `python main.py --apply ai` → `mode="ai", channel="email"` (unchanged default).
    - `python main.py --apply web` → CLI shorthand alias for `mode="ai", channel="web"` (tailored CV PDF + cover note + open URL + manual confirm). Passing `--apply web --no-ai` (or having no Gemini key) uses the built-in deterministic reordered tailored CV + template cover note fallback. Combining `--apply web --channel email` raises a CLI argument error; `--apply web --channel web` is accepted as a no-op.
    - Optional `--channel email|web|all` works with `--apply basic` or `--apply ai`:
      - `--channel email`: targets jobs with a usable contact e-mail.
      - `--channel web`: targets any eligible job with a URL (whether or not it also has an e-mail).
      - `--channel all`: uses `--limit` / `max_jobs_per_run` as a **shared combined cap**, iterating jobs sorted by `relevance_score DESC`; each job prepares via `email` when it has a usable contact e-mail, otherwise falls back to `web` (never preparing the same job twice).
    - Optional `--all-recent [DAYS]` (default 14 days when flag passed) loads deduplicated jobs across all recent sessions instead of only the single latest session.
- **Web/ATS Assist Preparation (`manager.prepare_web(job, mode="ai", ...)`):**
  - Eligibility & run caps mirror the selected `mode`:
    - `mode="ai"`: exclusions-only `EligibilityFilter` (`min_relevance_score=0`, `allowed_relevance_categories=[]`, `require_email=False`, requiring `job.application_url or job.source_url`) and `applications.ai.max_jobs_per_run` (10).
    - `mode="basic"`: full Basic `EligibilityFilter` (`require_email=False`, requiring URL) and `applications.max_jobs_per_run` (30).
  - Runs `JobAnalyzer` → `CVMatcher` (checks `min_match_score` in AI mode; or search `relevance_score` in basic mode) → `CVGenerator` (generates Tailored ATS PDF in `data/generated_cvs/` in AI mode, or attaches original PDF in basic mode) → `EmailGenerator.cover_note()` (plain, factual, fact-checked cover note for pasting into ATS forms / LinkedIn messages).
  - Creates `ApplicationRecord` with `mode=mode`, `channel="web"`, `recipient_email=None`, `status=PENDING_APPROVAL`, `sender_name=self.doc.identity.name`.
- **Web/ATS Assist Review Actions (`src/cli/apply_cli.py`):**
  - When reviewing a `channel == "web"` record:
    - Shows match breakdown, missing skills, concerns, CV changes, CV preview, Cover Note, and clickable `Application URL`.
    - Actions:
      1. `Open Application URL in browser` (opens `rec.job_url` via `webbrowser.open`)
      2. `Open CV PDF` (opens tailored/original PDF)
      3. `Open CV folder` (opens containing folder for easy drag-and-drop upload into ATS form)
      4. `Edit cover note` (Notepad / `$EDITOR`)
      5. `Regenerate CV` (in AI mode, with live `AIManager`!)
      6. `Mark as Applied (APPLIED_WEB)` (transitions `PENDING_APPROVAL → APPLIED_WEB`, records timestamp `sent_at`!)
      7. `Decide later (keep pending)`
      8. `Reject (not suitable)` (`closed_by='user'`)
      9. `Skip (do not apply)` (`closed_by='user'`)
      10. `Stop reviewing`
- **Fixes to `--review`, `--send-queue`, `_profile()`, and Post-Apply CLI:**
  - `--review`: calls `validate_applications_settings()`, loads the matching CV (`rec.original_cv_path` if present, else `choose_cv()`), initializes `AIManager` when AI mode pending apps exist and user consents (`ai_consent()`), so `"Regenerate CV"` truly uses AI!
  - `--send-queue`: resolves `sender_name` per record from `rec.sender_name or os.environ.get("APPLICANT_NAME") or ""` inside `EmailService.send_application()` so `From:` display name and Basic-mode attachment filename ALWAYS match what the user reviewed & approved!
  - `tracker.list([APPROVED])` orders by `COALESCE(match_score, 0) DESC, created_at ASC` (highest-scoring applications go out first!).
  - `_profile(db, cv, use_ai)`: if `use_ai=True` and stored profile has `analysis_method == "deterministic"`, upgrade it via `build_profile(cv, True)` and persist the upgraded profile!
  - Review prompt fix: when choosing `"Approve (queue for --send-queue)"`, ask `"Queue this application for --send-queue (to {recipient})?"` (and mention `[TEST redirect -> {test}]` if test mode is active); when choosing `"Edit e-mail"`, skip saving/withdrawing approval if subject & body are unchanged; parse `$EDITOR` with `shlex.split()`.
  - New CLI flags:
    - `--update-status <APP_ID> <STATUS>` (`interview`, `offer`, `employer_rejected`, `withdrawn`, `applied_web`) with optional `--note "..."`.
    - `--applications` shows `Channel` column, supports optional `--status <STATUS>` filter, and displays pipeline funnel summary.

---

### Pillar 5 — Reporting & Tracker Integration (`src/reporting/`)

- **`ReportData` (`src/reporting/report_models.py`):**
  - Add optional `application_statuses: dict[str, str]` (`job_key -> resolved status & channel`, e.g., `"SENT (email)"`, `"APPLIED_WEB (web)"`, `"INTERVIEW"`) and `applications: list[dict[str, Any]]` (global pipeline snapshot of non-system-auto-rejected applications from SQLite, up to 500 most recent ordered by `updated_at DESC`).
  - `application_statuses[job_key]` resolves multiple records for the same `job_key` by lifecycle precedence (`OFFER > INTERVIEW > SENT/APPLIED_WEB > APPROVED > PENDING_APPROVAL > FAILED > EMPLOYER_REJECTED > WITHDRAWN > user REJECTED/SKIPPED > system REJECTED/SKIPPED`), breaking ties by newest `updated_at`.
  - Populated automatically by `Orchestrator.run()` and `Orchestrator.regenerate_reports()` when the database has application records.
  - Add `email_applicable` count to `summary_counts()`.
- **`ExcelReportGenerator` (`src/reporting/excel_generator.py`):**
  - Add 3 columns right next to `Application URL`:
    - `Apply Channel` (`Email & Web`, `Email`, `Web/ATS`, `Manual/Unknown`)
    - `Application Email` (`", ".join(j.application_emails)`)
    - `Application Status` (from `d.application_statuses.get(j.job_id, "Not applied")`)
  - Add a new worksheet **`Application Tracker`** **only when `d.applications` is non-empty** (keeps first-run workbooks clean), listing tracked applications: `ID`, `Channel`, `Mode`, `Status`, `Score`, `Company`, `Job Title`, `Recipient / Apply URL`, `CV File`, `Updated`, `Sent / Applied`, `Notes`.
- **`DocxReportGenerator` (`src/reporting/docx_generator.py`):**
  - Show `Email-applicable opportunities` in Section 1 Executive Summary.
  - Show `Application e-mail` (clickable `mailto:` or plain text) and `Application status` in `_job_block()` when present.

---

### Pillar 6 — Single-Door Interactive Hub (`src/cli/hub.py` + `src/cli/application.py`)

- **New module: `src/cli/hub.py`** (`InteractiveHub`):
  - Launched automatically when running `python main.py` interactively with **no action flags** (`not any(action_flags)` and `app.interactive`).
  - Add `--search` flag to jump straight into the search flow without entering the hub (and `--defaults` continues running non-interactive search directly so scripts/tests never break!).
  - **Hub Header / Status Strip** (computed live from SQLite & Settings on each loop):
    - Latest search session: date, unique jobs, email-applicable count, web-applicable count.
    - Application funnel: `Pending Review`, `Approved Queue`, `Sent (Email)`, `Applied (Web)`, `Interview`, `Offer`.
    - Sending mode badge: `DRY RUN`, `TEST -> addr`, or `LIVE` (+ paused/emergency-stop alert if active).
  - **Hub Menu Choices:**
    1. **Search Jobs** — Runs `app.main_flow()`; on completion offers one-click transition `"Prepare applications from these results now?"`.
    2. **Browse & Filter Scraped Jobs** — Filter by category (`HIGHLY RELEVANT`/`RELEVANT`/All), channel (`Email`/`Web/ATS`/All), country, or keyword; displays Rich table with index; lets user pick a job to inspect full details or prepare a tailored application immediately!
    3. **Prepare Applications** — Prompt for channel (`Web/ATS Assist (Tailored CV + Cover Note)`, `E-mail Automation`, `All Channels`), mode (`AI (Tailored CV)` vs `Basic (Original PDF)`), scope (`Latest session` vs `All recent 14 days`), and limit; runs preparation and flows directly into Review.
    4. **Review Pending Applications** — Launches `ApplyCommands.review_pending()` (showing pending count badge).
    5. **Send Approved E-mail Queue** — Launches `ApplyCommands.send_queue()` (showing queued count badge).
    6. **Application Pipeline & History** — View table (filterable by status), update any application's stage (`Interview`, `Offer`, `Employer Rejected`, `Withdrawn`), or requeue `FAILED` items.
    7. **Setup, Health & Safety Controls** — Submenu for `Setup Check (--setup-check)`, `Source Health Check (--check)`, `Sending Status (--sending-status)`, `Gmail OAuth (--gmail-auth)`, `Emergency Stop`, `Resume Sending`, `Search History & Regenerate Reports`.
    8. **Exit**

---

## 6. File Impact Map

| File | Change |
|---|---|
| `src/cv/document.py` *(new)* | Canonical `CVDocument`, `ExperienceEntry`, `extract_identity`, `parse_experience`, `parse_skills`, `structure_cv`, `structure_cv_ai` (fixes `_verbatim_norm`, `ai_safe_dict`, continuation heuristic). |
| `src/applications/cv_document.py` | Thin backward-compatible re-export facade pointing to `src.cv.document`. |
| `src/cv/parser.py` | Multi-column-aware PDF text extraction, extended bullet normalization, DOCX hyperlink extraction. |
| `src/cv/analyzer.py` | Extended `ROLE_WORDS` & bullet stripping aligned with `cv/document.py`. |
| `src/cv/profile_builder.py` | Fix anti-fabrication guard in `merge_ai_profile()`, guard list types from AI JSON. |
| `src/ai/prompts.py` | Strengthen `redact_personal()` (candidate name + personal domains), update `application_email_prompt` / `cover_note_prompt`. |
| `src/utils/logger.py` | Recursive string-leaf redaction (`_redact_obj`) — zero JSON corruption/crashes. |
| `src/utils/validators.py` | Guard `urlsplit()` `ValueError` → `UnsafeURLError`; mojibake cleanup in `clean_text()`. |
| `src/utils/http_client.py` | Never retry non-transient error kinds; UTF-8 default for JSON bodies. |
| `src/extraction/content_parser.py` | Fix salary parser (`\b` on `_MULT`, funding/user-count context guard, sanity bounds, Indonesian `juta` salary context); fix `detect_seniority()` description false positives. |
| `src/extraction/html_extractor.py` | Preserve full text for email extraction prior to truncation; mojibake cleanup. |
| `src/extraction/job_normalizer.py` | Extract `application_emails` before truncating; consume `required_skills` & `application_instructions` in `ai_extract()`. |
| `src/extraction/browser_extractor.py` | Loop-safe async lock initialization & clean `aclose()`. |
| `src/intelligence/relevance.py` | Tighten `NON_TECH_TITLE` against GTM/Client Partner/Operations leakage. |
| `src/intelligence/verification.py` | Qualify Indonesian `biaya` scam heuristic to payment/fee terms only. |
| `src/intelligence/matching.py` | Period-normalized salary comparison (`hour`/`month`/`year`). |
| `src/discovery/query_generator.py` | Activate all hiring phrases, add dedicated e-mail query slice, fix target country rotation. |
| `src/sources/communities/hackernews.py` | Monthly-aware date window; untruncated text before normalizer. |
| `src/sources/job_portals/rss_feeds.py` *(new)* | `WeWorkRemotelySource` (official RSS feeds, stdlib XML). |
| `src/sources/job_portals/remote_feeds.py` | Add `WorkingNomadsSource`; keep untruncated text before normalizer. |
| `src/sources/ats_platforms/ats.py` | Keep untruncated text before normalizer; prioritize SEA boards when `wants_indonesia`. |
| `src/core/orchestrator.py` | Honor `use_ai: bool | None`; call `BrowserRenderer.aclose()`; attach application statuses/history to `ReportData`; run Verifier+Matcher in fallback path. |
| `src/storage/database.py` | Version-aware idempotent v3 schema migration (`channel`, `closed_by`, `sender_name`, `follow_up_at` + indexes). |
| `src/storage/repository.py` | Add `recent_opportunities(days=14)` for cross-session `--apply --all-recent`. |
| `src/applications/models.py` | Add `APPLIED_WEB`, `INTERVIEW`, `OFFER`, `EMPLOYER_REJECTED`, `WITHDRAWN`, `channel`, `closed_by`, `sender_name`, `follow_up_at`, updated `TRANSITIONS` & `BLOCKING_STATUSES`. |
| `src/applications/tracker.py` | `send_lock IS NULL` guard in `transition()`, single-transaction `set_content()`/`requeue()`, Gmail `+tag` normalization in `find_recipient_blocking()`, score-priority queue ordering, `update_pipeline_status()`, `latest_status_by_job()`. |
| `src/applications/contacts.py` | Prioritize corporate over freemail; filter freemail before picking best when `allow_freemail=False`. |
| `src/applications/eligibility.py` | Support `channel="web"` (`require_email=False`, require URL). |
| `src/applications/cv_matcher.py` | Include `doc.project_entries` bullets + `Preferences` / arrangement concerns. |
| `src/applications/cv_generator.py` | Accept `experience_years`, reorder `skill_groups` on AI `skills_order`, overwrite existing path on regenerate. |
| `src/applications/email_generator.py` | Pass `dates` & `projects` to AI, honor `email_language`, fix tone double-check false positives, add `cover_note()`, refine EN/ID templates. |
| `src/applications/smtp_client.py` | Route `SMTPRecipientsRefused` through `classify_smtp()`. |
| `src/applications/email_service.py` | CRLF `policy.SMTP`, `preflight` subject/body/placeholder checks, `reply_to` support, `rec.sender_name` preservation, single-Gmail test-redirect + clean domain counter. |
| `src/applications/setup_check.py` | Validate `recipient_cooldown_days`, cap `max_attachment_mb <= 18`, allow own address for `APPLICATION_TEST_RECIPIENT`. |
| `src/applications/manager.py` | Multi-channel `prepare_basic`/`prepare_ai`/`prepare_web`, cross-session `load_jobs(..., all_recent_days=...)`, fix `_already_closed()` with `closed_by`, preserve `sender_name`. |
| `src/reporting/report_models.py`, `excel_generator.py`, `docx_generator.py` | Add `Apply Channel`, `Application Email`, `Application Status`, and `Application Tracker` sheet. |
| `src/cli/questions.py` | Fix Indonesian salary thousands separator (`10.000.000`) & `extra_languages` persistence. |
| `src/cli/apply_cli.py` | Web/ATS review actions, `--apply web`, `--channel`, `--all-recent`, `--update-status`, `--status`, fix `--review` AI + CV matching, fix `--send-queue` sender name, fix confirmation wording & `shlex.split($EDITOR)`. |
| `src/cli/hub.py` *(new)* | Single-door Interactive Hub dashboard, job browser, and unified workflow launcher. |
| `src/cli/application.py` | Wire Interactive Hub for interactive flag-free runs, `--search`, `--ai-consent`, strict `--no-ai` propagation, clean DB/Repo usage. |
| `config/settings.yaml`, `config/search_profiles.yaml`, `config/email_templates.yaml` | Register new connectors, SEA seed boards, refined EN/ID templates, updated defaults/comments. |

---

## 7. What's Left to the Builder

- Exact Rich table column widths, colors, and formatting details in `src/cli/hub.py`.
- Helper function decomposition and internal naming inside each modified file.
- Exact test fixture construction in `tests/unit/` and `tests/integration/` (all external HTTP/SMTP/AI calls remain strictly mocked in offline tests).

---

## 8. Builder Check Status

Completed via fresh independent builder-review pass (`Spec Builder Reviewer`). All 5 surfaced edge cases were resolved and folded directly into §1.3, §4.2, §4.3, and §5:

1. **Orthogonal `mode` vs `channel`:** `mode` stays `"basic" | "ai"`, `channel` stays `"email" | "web"`, `--apply web` is a CLI alias for `mode="ai", channel="web"`, and `--apply web --channel email` is rejected as a CLI error.
2. **Blocking & cross-channel dedupe:** `EMPLOYER_REJECTED` and `WITHDRAWN` are included in `BLOCKING_STATUSES`; `make_dedupe_key()` falls back to `canonical_url(job_url)` when both company and recipient are empty; `find_blocking()` and `closed_by='user'` apply across channels while `closed_by='system'` is scoped to `(job_key, mode, channel, session_id)`.
3. **Web/ATS eligibility, shared `--channel all` cap, and queue guard:** `prepare_web()` mirrors `mode`'s eligibility filter and cap; `--channel all` enforces one shared cap sorted by `relevance_score DESC` (preferring `email` per job, falling back to `web`); `approve()` and `auto_approve()` refuse `channel == "web"`.
4. **Test-redirect rate-limit semantics:** `TestRedirectTransport` passes `recipient=None` to both `guard.check()` and `guard.record()`, preserving daily/hourly/interval/window/emergency-stop limits while keeping `max_per_recipient_domain_per_day` unpolluted.
5. **Report tracker scope & precedence:** `Application Tracker` sheet is a global non-system-rejected pipeline snapshot (up to 500 newest rows), omitted when empty; `application_statuses[job_key]` uses explicit lifecycle precedence with `updated_at` tie-break.
