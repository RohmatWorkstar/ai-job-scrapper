# Project Analysis: AI Job Intelligence & Discovery System

Analysis date: 2026-11-23 · Commit analysed: `ed52565` · Analysed before any change was made.

This document records how the existing project works, so that the new application automation is
added to that architecture rather than built beside it.

---

## 1. Summary

The project is a **local-first Python CLI** (not a website). It:

1. reads a CV from `cv/`,
2. builds an evidence-based candidate profile (deterministic analysis, plus Gemini when a key is set and the user consents),
3. asks ten search questions,
4. searches about 17 public job sources concurrently,
5. extracts, normalises, deduplicates, verifies and scores every opportunity,
6. stores everything in SQLite and writes DOCX, XLSX and JSON reports.

It has **no email, application or CV-generation functionality** today. The data, AI and storage layers
are well separated and can be reused directly.

| Item | Value |
|---|---|
| Python | 3.12.15 in `.venv` (the code requires 3.11 or newer, see `main.py`) |
| Entry point | `main.py` → `src.cli.application.run()` |
| UI | Rich (output) + Questionary (prompts) |
| HTTP | `httpx` wrapped by `src/utils/http_client.py::SafeHttpClient` (SSRF checks, robots.txt, per-host throttle, retry, cache) |
| Storage | SQLite at `data/database/job_intelligence.sqlite3` (WAL mode) |
| AI | Google Gemini REST API (`src/ai/gemini_provider.py`) behind `AIManager` (budget, cache, graceful fallback) |
| Tests | pytest. Offline suite passes now: 92 passed, 1 live test skipped |
| Package manager | No `pip` in the venv. `uv` is available on the machine. |

---

## 2. Directory structure

```text
main.py                      entry point (Python version guard, UTF-8 console, calls run())
config/
  settings.yaml              defaults, budgets, matching weights, timeouts, connectors list
  search_profiles.yaml       portals, ATS seed boards, role families, countries
  skills_taxonomy.yaml       technology names and aliases used for skill detection
cv/                          user CVs (git-ignored except README.md); currently EMPTY
src/
  cli/        application.py (flow + argparse), questions.py, display.py (Rich helpers)
  core/       configuration.py (Settings + Credentials from .env), models.py (dataclasses),
              orchestrator.py (pipeline), pipeline.py (progress state), exceptions.py
  cv/         parser.py (PDF/DOCX/TXT → text), analyzer.py (deterministic profile),
              profile_builder.py (AI merge with anti-fabrication rules), models.py
  ai/         base.py (AIProvider ABC), gemini_provider.py, provider_manager.py (AIManager), prompts.py
  discovery/  query_generator.py, source_registry.py, discovery_manager.py
  sources/    search_engine/, job_portals/, ats_platforms/, company_careers/, social_media/, communities/
  extraction/ html_extractor.py (JSON-LD, apply links), job_normalizer.py, content_parser.py, browser_extractor.py
  intelligence/ relevance.py (prefilter), verification.py, deduplication.py, matching.py, evidence.py
  storage/    database.py (schema), repository.py (data access), history.py (new-vs-seen, preferences)
  reporting/  report_models.py, docx_generator.py, excel_generator.py
  utils/      http_client.py, validators.py, date_time.py, logger.py (JSON lines + redaction), retry.py
tests/        unit/, integration/, fixtures/, test_pipeline.py, conftest.py (mock HTTP Router)
data/         created at runtime (database, cache, logs) - git-ignored
reports/      created at runtime - git-ignored
```

---

## 3. Main application workflow

`python main.py [--cv FILE] [--defaults] [--no-ai] [--max-queries N] [--check] [--history] [--report ID]`

1. `Application.environment()` loads settings and `.env`, sets up logging, creates the CV folder and
   opens the database to run the schema migration.
2. `choose_cv()` lists `cv/*.pdf|docx|txt` and parses the chosen file with `parse_cv()`.
3. `ai_consent()` asks before a redacted CV is sent to Gemini.
4. `ProfileBuilder.build()` runs `CVAnalyzer` (deterministic), then optionally merges a validated AI
   interpretation. AI output that the CV text does not support is dropped or labelled *transferable*.
5. The profile is saved to `candidate_profiles`.
6. The questionnaire produces a `SearchConfig`, and the answers are saved to `data/preferences.json`.
7. `Orchestrator.run()` runs the seven pipeline stages (§4) and writes the reports.

---

## 4. Scraping workflow (Orchestrator)

| Stage | Code | Notes |
|---|---|---|
| Query generation | `discovery/query_generator.py` | Budgeted queries per category and language (EN/ID) |
| Discovery | `discovery/discovery_manager.py` + connectors from `settings.yaml` | Each source is isolated, so one failure never stops the run. Raw results are persisted immediately. |
| Extraction | `_fetch_pages` → `html_extractor.fetch_and_extract` → `job_normalizer.enrich_with_page`; optional `ai_extract` | Page fetches respect robots.txt and the no-fetch domains (LinkedIn, Indeed, ...) |
| Normalisation | `JobNormalizer.normalise` → `JobOpportunity` | `job_description` is truncated to `extraction.max_description_chars` (6000) |
| Deduplication | `intelligence/deduplication.py` | |
| Verification | `Verifier` | VERIFIED_OPEN ... POSSIBLE_SCAM |
| Matching | `Matcher` (deterministic, explainable components) + optional `ai_rerank` | Score 0–100 plus category HIGHLY RELEVANT ... NOT RELEVANT |
| Persistence and reports | `Repository.save_opportunities`, DOCX/XLSX/JSON | |

**Supported sources:** Greenhouse, Lever, Ashby and SmartRecruiters APIs; Remotive, Remote OK,
Arbeitnow, Jobicy and Himalayas feeds; HN "Who is hiring"; Reddit API; X API; Brave web search (portals,
social and communities via `site:` queries); and company career pages (JSON-LD).

---

## 5. Data storage

SQLite, defined in `src/storage/database.py` (`SCHEMA_VERSION = 1`). Migration uses
`CREATE TABLE IF NOT EXISTS`; there is no ALTER-based migration mechanism yet.

| Table | Content |
|---|---|
| `candidate_profiles` | profile JSON per CV hash |
| `search_sessions` | one row per run: config, stats, report dir |
| `search_queries`, `raw_discoveries`, `source_runs`, `search_errors` | run metadata |
| `opportunities` | one row per unique job (`job_key`), last `data_json` = full `JobOpportunity` |
| `session_opportunities` | job snapshot per session (with relevance score and category) |
| `source_references`, `verification_history`, `reports` | provenance and audit |

The **input for application automation** is `opportunities.data_json` or `session_opportunities.data_json`.
They hold the title, company, description, skills, `application_url`, scores and verification status.
`JobOpportunity.from_dict` ignores unknown keys, so adding fields stays backward compatible with rows
that already exist.

---

## 6. CV storage

- Location: `cv/` (configurable as `candidate.cv_directory`). Git ignores everything in it except `README.md`.
- **The folder is currently empty.** No real CV is present, so development and tests use
  `tests/fixtures/sample_cv.txt` and generated fixture PDFs.
- Supported formats: `.pdf` (PyMuPDF, scanned-PDF detection), `.docx` (python-docx), `.txt`.
  **`.doc` is not supported.** `antiword` exists only inside the MSYS2 environment, and LibreOffice is
  not installed.
- The CV is never modified. Only its text is read, and its SHA-256 hash is stored.

---

## 7. Dependencies

Runtime: `rich`, `questionary`, `httpx`, `beautifulsoup4`, `pymupdf<2`, `python-docx`, `openpyxl`,
`PyYAML`, `python-dotenv`, `python-dateutil`, `tzdata`. Dev: `pytest`. Optional: `playwright`.

Relevant findings for the new features:

- **PyMuPDF is already installed and can generate PDFs** through its `Story` HTML/CSS layout API.
  Custom CVs can therefore be produced without a new PDF library.
- No Google client libraries are installed. The Gmail OAuth flow will need `google-auth` and
  `google-auth-oauthlib`. These are official libraries, and they will be imported lazily so the
  scraper keeps working without them.

---

## 8. Configuration and environment

- `.env` (git-ignored) holds the AI, search, Reddit and X keys. `Credentials.__repr__` masks the secrets.
- `config/settings.yaml` holds the non-secret behaviour settings. `Settings.get("a.b.c")` provides dotted access.
- `.gitignore` already excludes `.env*`, `cv/*`, `data/` and `reports/`.

---

## 9. Logging and error handling

- JSON-lines log in `data/logs/app_YYYYMMDD.log`. Registered secrets, API keys, bearer tokens,
  **e-mail addresses** and phone numbers are redacted. Recipient addresses therefore never reach the logs.
- An exception hierarchy (`JobIntelError` → `ConfigurationError`, `CVError`, `SourceError`,
  `AIProviderError`, `StorageError`, ...) separates permanent from transient errors.
- `utils/retry.py` provides bounded exponential backoff with jitter, used for transient HTTP failures.
- Per-source failure isolation. Partial results survive Ctrl+C.

---

## 10. Duplicated code, bugs and weaknesses found

| # | Finding | Impact on the new features | Action |
|---|---|---|---|
| 1 | `extract_from_html` **skips `mailto:` links**, and no model has an e-mail field | No recruiter e-mail is collected today | Collect mailto addresses and e-mails found in text, and add `JobOpportunity.application_emails` (backward compatible) |
| 2 | `job_description` is truncated to 6000 chars before storage | "Send your CV to ..." lines at the end of long posts can be lost | Extract e-mails from the full text **before** truncation |
| 3 | Most ATS/API sources (Greenhouse, Lever, ...) apply through web forms, not e-mail | Only a minority of jobs (Indonesian posts, HN, community and social posts, some career pages) will have an application e-mail | Report this clearly. Jobs without an e-mail are listed as SKIPPED with the reason. |
| 4 | Two `_EMAIL_RE`/redaction helpers (`ai/prompts.py`, `utils/logger.py`) | Minor duplication | Reuse; no refactor |
| 5 | `ai_consent()` returns **True in non-interactive mode when no answer was ever saved**, so `--defaults` can send the redacted CV to Gemini without an explicit "yes" | Privacy | Not changed (existing behaviour preserved). Recommended fix in the final report. |
| 6 | Schema migration uses only `CREATE IF NOT EXISTS` | New tables are safe. New columns on existing tables would need ALTER. | Add new tables only, and bump `SCHEMA_VERSION` to 2 |
| 7 | `Application` opens a new `Orchestrator` (and DB connection) only to read or write the repository | Minor inefficiency | Reuse `Database`/`Repository` directly in the new code |
| 8 | `.doc` CVs are unsupported | The specification asks for DOC "where practical" | Convert with `antiword` or LibreOffice when either is on PATH, otherwise show a clear error |

---

## 11. Recommended architecture for the new features

New package **`src/applications/`**, reusing the existing settings, the database, `AIManager`,
`SkillDetector`, `Matcher`, `parse_cv`, the logger and the retry helpers:

```text
src/applications/
  models.py            ApplicationStatus, ApplicationMode, ApplicationRecord, CandidateIdentity
  contacts.py          recipient e-mail extraction, validation and ranking (no-reply/platform filters)
  eligibility.py       configurable Basic-mode filters (score, category, verification, keywords)
  tracker.py           application_tracker: SQLite tables, statuses, duplicate keys, history
  sending_limits.py    daily/hourly caps, min interval, rolling windows, auto-pause, emergency stop
  gmail_client.py      OAuth 2.0 (installed-app loopback, token in data/secrets/), Gmail REST send, error classes
  email_service.py     MIME building, attachment validation, transports (Gmail / dry-run .eml outbox)
  email_generator.py   template e-mails (Basic) + AI e-mails validated against the CV (AI mode)
  job_analyzer.py      ai_job_analyzer: requirements, keywords, responsibilities (AI + deterministic)
  cv_document.py       cv_parser extension: structured CV (experience, bullets, skills, ...) from CV text
  cv_matcher.py        explainable job-to-CV matching (reuses Matcher + skill evidence)
  cv_generator.py      tailored CV content (reorder or rephrase only, validated) → ATS-friendly PDF
  scheduler.py         sending_scheduler: manual / scheduled / automatic queue processing
  manager.py           application_manager: Basic and AI flows end-to-end
src/cli/apply_cli.py   review screen: approve / reject / edit e-mail / regenerate CV / skip
```

The CLI is extended with new flags on `main.py`; the existing flags and the default flow stay unchanged.
Proposed flags: `--apply basic|ai`, `--session`, `--send-queue`, `--gmail-auth`, `--applications`,
`--emergency-stop`, `--resume-sending` and `--dry-run`.

The database gains new tables only (`applications`, `send_attempts`, `sending_control`). The existing
tables are not altered.

### Safety design principles

- **Approval is enforced in the data layer, not only in the UI.** A send requires status `APPROVED`
  and a stored content hash that equals the hash of the message being sent. Editing after approval
  therefore needs a new approval.
- **Dry-run by default in development.** The dry-run transport writes `.eml` files to an outbox and
  never contacts Gmail.
- **Duplicate key** = normalised company + normalised title (and recipient + title). A second application
  is refused unless the user explicitly authorises it, and that authorisation is recorded.
- **Quota or policy errors from Gmail pause all sending**, which then needs a manual resume. The blocked
  e-mail is not retried automatically.
- **Only the `gmail.send` scope** is requested (least privilege). The token is stored in `data/secrets/`,
  which is git-ignored.
- **The AI never adds facts.** Rewritten bullets and e-mails are checked: every technology term and every
  number must exist in the source CV, otherwise the original wording is used.
- **The original CV is read-only.** Generated CVs go to `data/generated_cvs/`.

---

## 12. Implementation risks

| Risk | Mitigation |
|---|---|
| Few scraped jobs carry an application e-mail | Extraction from text and mailto links, and clear SKIPPED reasons. Applying through ATS forms stays manual. |
| Gmail account restrictions or suspension from unsolicited bulk mail | Conservative defaults, documented limits, auto-pause, manual approval as the default, no account rotation |
| OAuth app in *Testing* status may issue refresh tokens that expire quickly | Documented. Re-authentication is handled with a clear message. |
| AI fabricates CV content | Validation against the CV text, with fallback to the original wording. Tests cover this. |
| No real CV or Gmail account during development | Fixtures, mock HTTP transports and dry-run mode. Live OAuth and sending are listed as **untested**. |
| PDF layout quality depends on the PyMuPDF Story HTML subset | Simple single-column ATS layout. The PDF is validated by re-opening it and extracting its text. |
| Contact e-mails may be scam signals (free-mail + fee wording) | Jobs that verification marks `POSSIBLE_SCAM` are never eligible |

---

## 13. Implementation plan (phases)

1. Data layer: `application_emails` extraction, `applications` tables, tracker, statuses and duplicate keys.
2. Sending safety: limits, counters, emergency stop, auto-pause.
3. E-mail: MIME builder, attachment validation, dry-run transport, Gmail OAuth and REST client with error classification and backoff.
4. Basic mode: eligibility, template e-mail, original PDF CV, review and approval, send, record.
5. AI mode: job analysis, structured CV, explainable matching, tailored CV PDF, AI e-mail, review actions.
6. Scheduler: manual, scheduled and automatic modes, all through the same safety gate.
7. CLI wiring, configuration (`settings.yaml` `applications:` section, `.env.example`), tests, then `FINAL_DEVELOPMENT_REPORT.md`.
