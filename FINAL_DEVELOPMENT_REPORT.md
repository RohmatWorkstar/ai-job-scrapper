# Final Development Report: Job Application Automation

Date: 2026-11-23 · Base commit: `ed52565` · Companion document: [PROJECT_ANALYSIS.md](PROJECT_ANALYSIS.md)

---

## 1. Executive Summary

The existing job-discovery CLI now has an **e-mail application module** (`src/applications/`). It is
built on the existing scraper, database, AI layer and CLI.

- **Scraper change:** the scraper now collects **application e-mail addresses** from job text and
  `mailto:` links. Before this change it discarded them.
- **Basic mode** (`--apply basic`):
  - filters jobs with configurable eligibility rules,
  - writes a template e-mail containing only facts from the CV,
  - attaches the **original CV PDF unchanged**,
  - waits for approval before anything is sent.
- **AI mode** (`--apply ai`):
  - analyses each job and computes an **explainable match score**,
  - generates a **tailored, ATS-friendly CV PDF** for each job,
  - writes a job-specific e-mail.
  - Every AI-written sentence is **fact-checked against the original CV**. Rewrites that add a
    technology, a number or a leadership claim are discarded, and the original wording is kept.
- **Review screen:** for each application you can approve, reject, edit the e-mail, regenerate the
  CV or skip.
- **Gmail sending:** OAuth 2.0 with the `gmail.send` scope only, error handling as Google documents
  it, and bounded exponential backoff.
- **Sending safety:** rolling daily and hourly caps, a minimum interval between e-mails, a cap per
  recipient domain, a sending window, automatic pauses, and an emergency stop.
  - Values above half of Google's documented limits are refused.
- **Persistent history:** every application and every status change is kept in the existing SQLite
  database, and duplicate applications are prevented.
- **Dry run is the default.** Until you switch it off, nothing is sent: e-mails are written as `.eml`
  files to `data/outbox/`.

**Test status:** 165 offline tests pass and 1 opt-in live test is skipped. Before this work there
were 92 tests. A post-implementation review fixed 18 issues; see §14.

**Not tested against real services:** live Gmail OAuth and sending, and real Gemini responses for
the new prompts. See §9.

---

## 2. Existing Project Analysis

See [PROJECT_ANALYSIS.md](PROJECT_ANALYSIS.md). The original architecture was kept:

| Existing component | How it was reused |
|---|---|
| `Settings` / `settings.yaml` / `.env` | New `applications:` section. Gmail and applicant variables are read from the environment. |
| SQLite `Database` | New tables only (schema v2). No existing table was altered. |
| `AIManager` + Gemini provider | The same budget, cache, consent and redaction rules. The new prompts use the existing untrusted-data fence. |
| `parse_cv`, `CVAnalyzer`, `token_overlap` | CV text, profile and validation helpers |
| `SkillDetector`, `Matcher` | Technology detection; the title and experience components of the match score |
| `JobNormalizer`, `html_extractor`, `deduplicate` | E-mail extraction added in place |
| `utils/retry.backoff_delay`, `utils/logger` | Backoff, plus JSON logs (e-mail addresses are redacted in the logs) |
| `cli/application.py` argparse | New flags were added. The existing flags and the default `python main.py` flow are unchanged. |

### Pre-existing bug found and fixed

- **Symptom:** Questionary's `.ask()` starts its own event loop and raises
  `RuntimeError: asyncio.run() cannot be called from a running event loop` when it is called inside
  a coroutine.
- **Where:** `Application.main_flow()` (async) called `choose_cv()`, `ai_consent()` and
  `questionnaire()`, which all prompt. In a real interactive terminal these prompts would therefore
  have failed.
- **Why the existing tests missed it:** they drive the prompts outside an event loop.
- **Fix:** three lines in `main_flow` now run those steps through `asyncio.to_thread(...)`.
- **Regression test:** `test_prompts_work_from_inside_the_event_loop`.
- **How it was confirmed:** with a minimal reproduction (`questionary.select().ask()` inside
  `asyncio.run`). An interactive search was **not** run end-to-end in a real terminal.

---

## 3. New Features

| Feature | Module |
|---|---|
| Application e-mail extraction (text, `[at]/[dot]` obfuscation, `mailto:`), ranked by "apply/kirim lamaran" context on the same line, kept before the description is truncated | `src/extraction/contact_parser.py`, `html_extractor.py`, `job_normalizer.py`, `deduplication.py` |
| Recipient validation: syntax, no-reply/system mailboxes, job platforms, example domains, your own address; free-mail flagged; the company website domain is preferred | `applications/contacts.py` |
| Configurable eligibility filters | `applications/eligibility.py` |
| Application tracker: statuses, allowed transitions, compare-and-set updates, send lock, events, duplicate keys | `applications/tracker.py` |
| Sending limits, automatic pause and emergency stop | `applications/sending_limits.py` |
| Gmail OAuth and REST client with error classification | `applications/gmail_client.py` |
| MIME builder, attachment validation, dry-run and Gmail transports, **single send gate** | `applications/email_service.py` |
| Manual, scheduled and automatic queue processing | `applications/scheduler.py` |
| Structured CV (identity, experience entries, bullets) and `.doc` support | `applications/cv_document.py`, `cv/parser.py` |
| Job analysis (deterministic plus validated AI) | `applications/job_analyzer.py` |
| Explainable matching | `applications/cv_matcher.py` |
| Fact checking of AI text | `applications/fact_check.py` |
| Tailored CV and PDF rendering/validation | `applications/cv_generator.py` |
| Template and AI e-mails | `applications/email_generator.py`, `config/email_templates.yaml` |
| End-to-end flows | `applications/manager.py` |
| Review UI and commands | `src/cli/apply_cli.py` |

---

## 4. Application Modes

### Mode A: Basic (`python main.py --apply basic`)

1. Loads the latest search session, or the one given with `--session ID`.
2. For each job, best relevance first, it checks:
   - whether the posting has an application e-mail,
   - eligibility: relevance score ≥ 58; category HIGHLY RELEVANT or RELEVANT; not CLOSED,
     POSSIBLE_SCAM or INACCESSIBLE; no excluded title keyword; posting no older than 45 days.
     All of these are configurable.
   - recipient validity,
   - duplicates.
3. It writes a template e-mail (English or Indonesian, chosen from the posting). The only facts used
   are your current title, your years of experience and skills that the CV proves.
4. It attaches the **original CV PDF**, unchanged. The PDF is validated, and its SHA-256 hash is
   recorded at approval.
5. The record is shown for review. Nothing is sent before you approve it.
6. Results:
   - Ineligible jobs → `REJECTED`, with reasons.
   - Unusable addresses → `SKIPPED`.
   - Jobs without an e-mail are counted only; they have to be applied for on the website.

Basic mode needs a **PDF** CV: the selected file, a PDF with the same name, or
`applications.basic_cv_file`.

### Mode B: AI analysis and custom CV (`python main.py --apply ai`)

| Step | What happens | Status |
|---|---|---|
| Pre-filter | Has an e-mail, not scam or closed, no excluded keyword. No AI calls are spent on these jobs. | |
| Job analysis | Deterministic sections (responsibilities, requirements), skills and years. The AI adds structure, but only items supported by the posting text are kept. | `ANALYZED` |
| CV analysis | The CV is segmented into experience entries, bullets, skills, education, projects, certifications and languages. AI segmentation is accepted only when its lines are found verbatim in the CV. | |
| Matching | Explainable score (§7). Below `ai.min_match_score` (55) the job is `REJECTED` and no CV is generated. | `MATCHED` / `REJECTED` |
| Custom CV | Reordered and validated content, rendered as a PDF at `data/generated_cvs/<Company>_<JobTitle>_Custom_CV.pdf` (sanitised name). The PDF is re-opened and validated. | |
| E-mail | The AI e-mail is fact-checked. On failure the template is used and the reason is noted. The signature is added locally. | `PENDING_APPROVAL` |
| Review | Shows company, title, recipient, score, components, matched and missing skills, relevant experience, concerns, CV changes, rejected AI rewrites, CV path with page-1 preview, subject, body and attachment name. Actions: approve, reject, edit e-mail, regenerate CV, skip. | |
| Send | Through the send gate. The generated PDF is attached and the record is updated. | `SENT` / `FAILED` |

Without a Gemini key or consent, AI mode still runs. Analysis is then deterministic and the CV is
only reordered; the review screen says so.

---

## 5. Email Automation

### Gmail integration

- **OAuth:** installed-app flow with a loopback redirect, run by the official `google-auth-oauthlib`
  library (`python main.py --gmail-auth`).
- **Scope:** only `https://www.googleapis.com/auth/gmail.send`. The tool cannot read your mailbox.
- **Token:** stored in `data/secrets/gmail_token.json`, which is git-ignored, and refreshed
  automatically. When refresh fails, you get a clear "run `--gmail-auth`" message.
- **Send endpoint:** `POST https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=media`
  with `Content-Type: message/rfc822` (documented maximum 35 MB). Attachments are capped at 10 MB
  by configuration.
- **Credentials:** nothing is hard-coded. The client JSON, token and sender come from `.env` or
  `data/secrets/`, and `.gitignore` also excludes `client_secret*.json`, `*gmail_token*.json` and `*.eml`.

### The send gate (`EmailService.send_application`, the only code path that sends)

The gate refuses to send unless all of the following hold:

1. The status is `APPROVED`, and the approval hash equals the hash of recipient + subject + body +
   attachment path + attachment SHA-256. Any edit, a regenerated CV, or a CV file changed on disk
   therefore needs a new approval.
2. The guard allows it: emergency stop, pause, sending window, daily, hourly, interval and domain caps.
3. The recipient and the PDF validate.
4. The record's send lock is claimed atomically, so it cannot be sent twice.

### Error handling (from Google's error guide)

| Gmail response | Action |
|---|---|
| 429 "… (Mail sending)" | `FAILED`. All sending pauses for 24 h. **Never retried.** |
| 403 `dailyLimitExceeded` | `FAILED`, pause 24 h, not retried |
| 403 `domainPolicy` / other 403 | `FAILED`, pause until you resume manually |
| 403 `rateLimitExceeded` / `userRateLimitExceeded`, other 429, 5xx | Exponential backoff with jitter, honouring `Retry-After`; up to 3 attempts. If it persists: rate limit → pause; 5xx → `FAILED`. |
| 401 | One token refresh, then `GmailAuthError` (sending pauses) |
| 400 (for example an invalid recipient) | `FAILED`, not retried. 3 invalid recipients in 24 h pause sending. |
| Connection never established | Retried |
| Timeout or connection drop **after** the upload started | `FAILED` with `delivery_unknown`. The lock is kept and nothing is retried; a duplicate application is also blocked. Check the Gmail *Sent* folder (the Message-ID is stored) before running `--requeue`. |
| Any failures in a row (default 3) | Automatic pause |

Network failures, failed attachments, invalid or missing addresses, duplicates and quota errors
are all covered by the tests (§9).

---

## 6. Gmail Safety Analysis

Official documentation was checked on **2026-11-23**. The numbers below are quoted from these pages.

| Source | Verified statement |
|---|---|
| [Limits for sending & getting mail](https://support.google.com/mail/answer/22839) (personal Gmail) | You may see "You have reached a limit for sending mail" if you send "more than 500 emails sent in a day". Sending is restored "within 1 to 24 hours". Many bounces can also trigger it. |
| [Personal Gmail limit](https://support.google.com/mail/answer/6596) | "personal Gmail (@gmail.com) accounts have a limit on the number of emails you can send per day." |
| [Gmail sending limits in Google Workspace](https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace) (updated 2026-11-10) | 2,000 messages/day per user (trial: 500). 10,000 total recipients/day. 3,000 external recipients/day. Applied over a **rolling 24-hour period**. Exceeding blocks sending "for up to 24 hours". "Sending limits can change without notice." |
| [Gmail API usage limits](https://developers.google.com/workspace/gmail/api/reference/quota) | 1,200,000 quota units per minute per project, 6,000 per minute per user. `messages.send` costs 100 units. 500 recipients per message. |
| [Gmail API error handling](https://developers.google.com/workspace/gmail/api/guides/handle-errors) | Mail-sending limits are "shared by all of the user's clients, whether API clients, built-in or web clients, or SMTP MSA". The error is 429 "User-rate limit exceeded (Mail sending)". "You can't increase per-user limits." "You can't assume that a 200 response means the email was successfully sent." |
| [Email sender guidelines](https://support.google.com/a/answer/81126) | All senders: SPF or DKIM, spam rate below 0.3% (aim for below 0.1%). "Don't send messages to people who didn't sign up to get messages from you." |
| [Gmail Program Policies](https://support.google.com/mail/answer/16734397) (updated 2026-10-20) | "Don't use Gmail to distribute spam or unsolicited commercial mail." No misleading automation. "Google may disable accounts that are found to be in violation of our policies." |
| [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) | `gmail.send` is a **sensitive** scope (not restricted) |
| [OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2) | An app in **Testing** status gets refresh tokens that **expire in 7 days**. Gmail-scoped tokens are revoked when the password changes. |
| [Unverified apps](https://support.google.com/cloud/answer/13464323) | Testing-mode apps are not subject to verification. Apps with a limited audience (fewer than 100 users) can run with the unverified-app warning. |

### API quota vs sending limits

They are different limits:

- **API quota** (units per minute) is a per-project and per-user request budget. A single user sending
  a few e-mails a day never comes close to it.
- **The mail-sending limit** is an anti-abuse limit on the *account*. It is shared with the web UI and
  SMTP and cannot be raised.

The guard enforces the second one conservatively.

### Not verified

- **No official page gives an API- or SMTP-specific daily number for personal accounts.**
- No page states that exceeding the limit by itself causes suspension. Suspension is tied to policy
  violations such as spam or unsolicited mail.

### Assumptions and implemented limits

- **Account type** defaults to `personal`. Documented ceilings are personal 500, Workspace 2,000 and
  trial 500. These are the points where **Google blocks sending**, not safe targets.
- Configuration is **refused above half of the ceiling** (250 for personal, 1,000 for Workspace). A
  warning appears above 50 per day.

| Control | Default |
|---|---|
| Max per rolling 24 h | 20 |
| Max per rolling hour | 6 |
| Minimum interval | 180 s |
| Max per recipient domain per 24 h | 2 |
| Sending window | 08:00–18:00 Asia/Jakarta, Monday–Friday |
| Consecutive failures → pause | 3 |
| Invalid recipients in 24 h → pause | 3 |
| Pause after a quota or sending-limit error | 24 h |

- Failed and blocked attempts count against the caps, because they may have reached Gmail.
- Counters are stored in the database, so they survive restarts.
- Dry-run counters are separate from real Gmail counters.
- **Emergency stop:** `--emergency-stop` (database flag) **or** create the file `data/STOP_SENDING`.
  The file works even from another terminal while a queue is running. Clear both with `--resume-sending`.
- **Never implemented:** account rotation, limit evasion, or repeated retries of blocked e-mails.

### Risk assessment and recommended configuration

- Job applications are individually addressed and relevant. Still, a recruiter may see them as
  **unsolicited mail**. The main account risk is **spam reports and bounces**, not the numeric cap.
- With the `gmail.send` scope this tool cannot see bounces. Google also says a 200 response does not
  guarantee delivery. **Check your inbox for bounce messages.**

| Account | Recommendation |
|---|---|
| Personal Gmail | Week 1: `max_per_day: 10`. Then 20. Keep `max_per_hour ≤ 6` and the interval ≥ 180 s, and review every e-mail (manual mode). |
| 100 applications/day | Treat as a ceiling, not a goal. Only consider it with a Google Workspace account on your own domain with SPF/DKIM/DMARC, a clean sending history, and very low bounce and complaint rates. Raise the cap gradually (for example 20 → 40 → 70 → 100 over several weeks). |
| Real volume | Limited by how many postings carry an application e-mail. Most ATS jobs (Greenhouse, Lever, …) accept applications only through web forms. |

---

## 7. AI CV Generation

### Matching

The score is 0–100 and fully explainable. These are the default weights, configurable in
`applications.ai.match_weights`:

| Component | Weight | How it is computed |
|---|---|---|
| skills | 40 | 0.8 × required skills evidenced in the CV + 0.2 × preferred skills evidenced. Implied skills (PostgreSQL → SQL) are labelled as implied. A skill counts only if the CV text contains it. |
| title | 25 | Existing `Matcher.position` |
| experience | 20 | Existing `Matcher.experience`, plus a cap when the posting asks for more years than the CV shows |
| keywords | 15 | Share of job keywords literally present in the CV |

The review screen also shows:

- matched skills (with CV evidence),
- missing required and preferred skills,
- relevant experience entries and projects,
- recommended section order,
- concerns, such as an unverified posting, location restrictions, or more than half of the
  required technologies missing.

### Custom CV

- **Source of truth:** the original CV text. The file in `cv/` is never written to (a test checks
  its SHA-256).
- **Deterministic tailoring:**
  - job-relevant skills first,
  - bullets reordered by relevance within each job,
  - relevant projects first.
  - Experience entries stay in the CV's chronological order, and the headline stays your own CV
    title (never the job title).
- **AI tailoring (optional):**
  - What the AI receives: only `CVDocument.ai_safe_dict()`, with no name, e-mail, phone, links or
    location.
  - What it may do: rephrase bullets and the summary, and reorder skills (only skills that exist in
    the CV).
  - Each rewrite is rejected when it:
    - introduces a technology not in the CV,
    - introduces a number not in the source bullet,
    - adds leadership or scope claims (led, managed, architected, …) the source does not make,
    - drifts from the CV wording,
    - grows by more than 1.6× (+40 characters),
    - contains links, e-mail addresses or placeholders.
  - Rejected rewrites are listed in the review screen.
- **PDF:**
  - A4, single column, standard headings (Summary, Skills, Experience, Projects, Education,
    Certifications, Languages), real text with no images or tables, so it is ATS-friendly.
  - Rendered with PyMuPDF's Story API, which is already a dependency.
  - Validated after writing: it opens, is not encrypted, has 1–3 pages and more than 200 characters
    of extractable text, and contains your name, the section headings and at least 80% of the skills.
- **Filename:** `Company_JobTitle_Custom_CV.pdf`. The name is ASCII-only, other characters become
  `_`, and each part is at most 50 characters. If the name already exists, `_<id>` is appended.

---

## 8. Database Changes

The schema version went from 1 to 2. **New tables only**; existing databases upgrade in place
through `CREATE TABLE IF NOT EXISTS`.

| Table | Purpose / key columns |
|---|---|
| `applications` | `id`, `job_key`, `session_id`, `company_name`, `job_title`, `job_url`, `recipient_email`, `mode`, `match_score`, `match_json` (analysis, match, CV changes), `original_cv_path`, `original_cv_sha256`, `generated_cv_path`, `attachment_path`, `attachment_sha256`, `email_subject`, `email_body`, `status`, `approval_status`, `approved_at`, `approved_by`, `approved_hash`, `dedupe_key`, `allow_duplicate`, `duplicate_authorization`, `send_lock`, `attempts`, `error`, `error_kind`, `gmail_message_id`, `message_id_header`, `transport`, `notes_json`, `created_at`, `updated_at`, `sent_at` |
| `application_events` | Audit trail of every status change: from, to, detail, time |
| `send_attempts` | Every attempt: time, outcome (sent/failed/blocked), transport, recipient domain, error. This table is the source of the rolling counters. |
| `sending_control` | `emergency_stop` and `paused` (reason and expiry) |

### Statuses

- **Statuses:** `NEW`, `ANALYZED`, `MATCHED`, `REJECTED`, `PENDING_APPROVAL`, `APPROVED`, `SENT`,
  `FAILED`, `SKIPPED`.
- **Transitions:** enforced in `models.TRANSITIONS`. `SENT`, `REJECTED` and `SKIPPED` are terminal.
  `FAILED` returns only to `PENDING_APPROVAL` (re-approval).

### Duplicate prevention

- **Key:** normalised company + normalised title. When the company is unknown, the recipient domain
  is used instead.
- **What blocks a new application:** a record with the same key in an active state or `SENT`, or one
  with an unknown delivery result.
- **Override:** `--allow-duplicate` plus an interactive confirmation. The confirmation is stored in
  `duplicate_authorization`.

### Other changes

- `JobOpportunity.application_emails: list[str]`. It is backward compatible, because old JSON
  without the field still loads.
- New method `Repository.get_opportunity(job_key)`.

---

## 9. Testing Results

Command: `python -m pytest` → **165 passed, 1 skipped** (the opt-in live network test). The new tests
are in `tests/unit/test_applications.py` and `tests/integration/test_application_flow.py`.

### Successful tests

| Area | Verified |
|---|---|
| **Basic mode** | CV detection and PDF requirement; eligibility filtering (score, category, scam, keywords, no e-mail); template e-mail (EN/ID; no unproven skills); attachment validation; approval workflow; dry-run send; duplicate prevention on a second run; original CV unchanged; non-interactive CLI end-to-end |
| **AI mode** (fake AI provider) | Job-description extraction and AI validation; CV parsing and segmentation; matching components; tailored PDF generation and validation; fabricated summary and bullet rejected; contact details never in AI prompts; AI e-mail accepted or rejected with template fallback; status trail NEW→ANALYZED→MATCHED→PENDING_APPROVAL; low score → REJECTED; regenerating the CV withdraws approval |
| **Gmail** (mocked HTTP) | Media-upload endpoint and headers; 429 mail-sending not retried; 403 daily limit / domain policy; rate-limit and 5xx backoff, then success; persistent errors; 401 refresh; connect error retried; read timeout → delivery unknown, not retried; token loading; expired-token refresh failure message; missing client file |
| **Safety** | Interval, hourly, daily (rolling) and domain caps with a fake clock; sending window; per-transport counters; emergency stop (flag and file); automatic pause after consecutive failures and invalid recipients; timed pause expiry; config refuses unsafe values; quota error pauses the queue; unapproved, edited-after-approval or changed-attachment applications cannot be sent; send lock prevents double sends; the queue stops at the daily cap and resumes after 24 h |
| **Interactive review** | Real Questionary prompts driven by simulated keys: approve and send, reject |
| **Scraper integration** | mailto collection, e-mails kept beyond description truncation, old JSON compatibility; all 92 original tests still pass |

### Failed tests

None remaining. Defects found during development and fixed:

- the temporary PDF was locked on Windows,
- a stray `(` in company names,
- a cross-line e-mail ranking error,
- Questionary inside a running event loop (also present in the original code, §2).

### Untested functionality

- **Live Gmail:**
  - the OAuth consent flow (`--gmail-auth`) and real sending. No real e-mail was sent, as instructed.
  - Google libraries are used for OAuth, and the REST calls follow the documentation; both are
    tested only with mocks.
  - Run a first live test by sending one application to an address you own.
- **Real Gemini responses** to the four new prompts (only a fake provider was used).
- **Interactive actions not exercised with simulated keys:** *Edit e-mail* (multiline editor),
  *Regenerate CV*, and the live-send confirmation prompt.
- **`.doc` conversion** with a real antiword or LibreOffice installation. Only the "no converter"
  path is tested.
- **CV structuring:** tested only on the sample text CV. Complex PDF layouts (multi-column,
  tables) may segment poorly; the review preview shows the result.

### Remaining issues

- Gmail recommends retrying 5xx errors. A 5xx after Gmail actually accepted the message could, rarely,
  produce a duplicate.
- With the `gmail.send` scope the tool cannot see bounces or replies.

---

## 10. Configuration Guide

### Gmail option A: App Password over SMTP (no Google Cloud project, no credit card)

1. Use a **personal @gmail.com** account and turn on **2-Step Verification**.
2. Create an App Password at <https://myaccount.google.com/apppasswords>.
3. Put it **yourself** in `.env` as `GMAIL_APP_PASSWORD=...` (spaces are fine), together with
   `GMAIL_SENDER_ADDRESS=you@gmail.com`. Never paste it in a chat, ticket or commit.
4. Leave `applications.sending.transport: auto`. SMTP (`smtp.gmail.com:587`, STARTTLS) is used
   automatically when the App Password is set.

Run `python main.py --setup-check`: it shows "set (value hidden)" and never prints the password.

**Security trade-off:** an App Password grants **full** access to the Google Account. Option B
(`gmail.send` only) grants much less. If an App Password leaks, delete it on the same page and create
a new one.

Sending limits, approval, the auto-pause rules and the emergency stop are identical for both options.
Gmail's limits are per account, so both options share the same counters.

### Gmail option B: Gmail API with OAuth (one-time, preferred when possible)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project. Under
   **APIs & Services → Library**, enable the **Gmail API**.
2. Under **Google Auth Platform / OAuth consent screen**, choose user type **External** (personal
   Gmail) and add your own address as a **test user**.
   - **Testing** status: refresh tokens expire after **7 days**, so you run `--gmail-auth` again
     weekly.
   - Alternatively, publish the app *In production* for personal use. You will then click through
     Google's "unverified app" warning.
3. Under **Clients**, create an OAuth client ID of type **Desktop app**. Download the JSON and save
   it as `data/secrets/gmail_client_secret.json`, or point `GMAIL_CLIENT_SECRET_FILE` at it.
4. In `.env`, set `GMAIL_SENDER_ADDRESS=you@gmail.com`. You can also set the optional
   `APPLICANT_NAME`, `APPLICANT_PHONE` and `APPLICANT_LINKS`; otherwise they are read from the CV
   header.
5. Run `python main.py --gmail-auth`. The browser opens; sign in and allow **Send email on your
   behalf**. The token is saved in `data/secrets/gmail_token.json`. Never share or commit it.
6. After checking dry-run output in `data/outbox/`, set `applications.sending.dry_run: false` in
   `config/settings.yaml`.

### AI

Use the existing `GEMINI_API_KEY` (plus the optional `AI_MODEL`) in `.env`. AI mode asks for the same
CV consent as the search flow. The budget per run is set by `applications.ai.max_calls_per_run`
(default 60), and `applications.ai.rewrite_with_ai: false` turns rephrasing off (reorder only).

### CV directories

| What | Where |
|---|---|
| Original CVs | `cv/` (`candidate.cv_directory`). Keep a PDF version for Basic mode, or set `applications.basic_cv_file`. |
| Generated CVs | `data/generated_cvs/` (`applications.generated_cv_directory`) |
| Dry-run e-mails | `data/outbox/` |

### Sending settings

These live in `config/settings.yaml` → `applications.sending`:

- `account_type`, `max_per_day`, `max_per_hour`, `min_interval_seconds`,
  `max_per_recipient_domain_per_day`, `sending_window`
- `mode`: `manual` | `scheduled` | `automatic`
- `automatic_enabled`, `automatic_min_match_score`

**Automatic mode** needs all three of: `mode: automatic`, `automatic_enabled: true`, and the `--auto`
flag on the command line. It is off by default.

Eligibility filters are in `applications.eligibility`. The e-mail wording is in
`config/email_templates.yaml`.

---

## 11. How to Run

```powershell
.venv\Scripts\activate
pip install -r requirements.txt            # adds google-auth, google-auth-oauthlib
                                           # (this environment has no pip; uv was used:
                                           #  uv pip install --python .venv\Scripts\python.exe -r requirements.txt)

python main.py                             # 1. search jobs (unchanged)
python main.py --setup-check               # what is configured / what you still need to do
python main.py --apply basic               # 2. Basic mode: prepare + review (dry run by default)
python main.py --apply ai                  #    or AI mode: tailored CV per job
python main.py --apply ai --limit 5 --session 2026-11-23_100000-abc123
python main.py --review                    # review applications still pending
python main.py --send-queue                # scheduled mode: send approved ones within the limits
python main.py --send-queue --limit 3      # send at most 3 now
python main.py --applications              # history
python main.py --sending-status            # counters, limits, pause/stop
python main.py --emergency-stop            # stop everything (or create data\STOP_SENDING)
python main.py --resume-sending
python main.py --requeue <APPLICATION_ID>  # FAILED -> PENDING_APPROVAL (asks you to check Gmail Sent first if delivery is unknown)
python main.py --gmail-auth                # OAuth consent
python -m pytest                           # tests
```

To run the scheduled mode periodically, use Windows Task Scheduler. For example, every 30 minutes
on weekdays, run `"<project>\.venv\Scripts\python.exe" "<project>\main.py" --send-queue --defaults`.

---

## 12. Remaining Tasks

1. A supervised live test: authorise Gmail, set `dry_run: false`, and send **one** application to an
   address you own.
2. Run `--apply ai` with a real Gemini key on a few jobs, then check the CV changes and the rejected
   rewrites.
3. Optional bounce awareness. It needs the `gmail.readonly` scope (a *restricted* scope, which brings
   stricter verification), so it is deliberately not included.
4. An application-history export in the XLSX report, if wanted.
5. `.doc` testing with a real converter, if you use `.doc` CVs.

---

## 13. Recommendations

- **Keep manual approval.** Automatic mode trades your review for volume, and wrong or irrelevant
  e-mails create the spam reports that endanger the account.
- **Ramp up slowly** (§6), and check the inbox for bounces every day. When anything looks unusual,
  use `--emergency-stop`.
- **Prefer a Google Workspace account on your own domain** (SPF/DKIM/DMARC) if you need higher
  volume. Never rotate accounts to get around limits.
- **Fix the existing `ai_consent` default.** In non-interactive mode (`--defaults`) it sends the
  redacted CV to Gemini when no answer was ever saved. Asking once interactively, or defaulting to
  `False`, would be safer. It was not changed, to preserve existing behaviour.
- **Back up `data/database/job_intelligence.sqlite3`.** It is the application history and the source
  of the sending counters.
- **Keep `data/secrets/` private.** Revoke access at <https://myaccount.google.com/permissions> if the
  token leaks.

---

## 14. Post-implementation review and fixes (2026-11-23)

An independent code review plus a spec and UX pass found the issues below. All are fixed and covered
by regression tests.

### Bugs

| # | Severity | Bug | Fix |
|---|---|---|---|
| 1 | High | A dry-run "send" marked the application `SENT` (final), so it could never be sent for real | Dry run is now a rehearsal: the `.eml` is written, the record returns to `PENDING_APPROVAL`, and the approval is withdrawn. A real send needs a new approval with dry run off. |
| 2 | High | An approved application whose CV file changed was stuck: refused at send time and impossible to re-review | Approval re-hashes the attachment as it is at that moment. A mismatch at send time returns the record to review. `--requeue` accepts it. |
| 3 | High | AI CV segmentation accepted invented bullets (fuzzy substring match: "led" inside "skilled", numbers ignored) | AI segmentation is accepted only when it occurs verbatim in the CV |
| 4 | Med-high | The AI e-mail could claim technologies named only in the job title; numbers written as words were not checked | The title and company strings are removed before the technology check; number words are checked; word matching is whole-word |
| 5 | Med-high | A footer `mailto:` (for example a portal's info@ address) outranked "send your CV to hr@…" | Mailto links are collected only from the main content and get no automatic priority |
| 6 | Medium | Ctrl+C during AI preparation left the record in a duplicate-blocking state forever | An interrupted preparation is marked `FAILED (interrupted)` |
| 7 | Medium | Unrelated employers using Gmail/Yahoo addresses blocked each other as duplicates | Free-mail recipients use the full address in the duplicate key |
| 8 | Medium | Editing during a send could leave a stuck record | Edits are refused while a send lock is held |
| 9 | Low-med | Errors while building the message after the lock was taken left a stuck lock | The message is built before the lock is claimed |
| 10 | Medium | `--requeue` after an unknown delivery kept the send lock, so the record could never be sent again | `requeue` releases the lock |
| 11 | Medium | An expired or revoked Gmail token marked the application `FAILED` (re-approval needed) although nothing was sent | The application stays `APPROVED`, sending pauses, and `--gmail-auth` clears that pause automatically |
| 12 | Low | A generated CV overwrote the attachment before validation | Render to a candidate file, validate, then replace |
| 13 | Low | Invalid sending windows (start ≥ end, empty weekdays, text hours) were accepted | Validated |
| 14 | Low | The "own address" recipient check was skipped in `--review` / `--send-queue` | Falls back to `APPLICANT_EMAIL` / `GMAIL_SENDER_ADDRESS` |

### Validation and UX additions

| Addition | Purpose |
|---|---|
| `python main.py --setup-check` | A checklist of what is set up and what the user must still do, with how to fix each item |
| Validation of the whole `applications:` settings section | Types, ranges, categories, statuses, modes and weights, with one combined error message |
| Review screen: Basic mode now shows the job's responsibilities and requirements (or a description excerpt) and the posting status | The user knows what they approve |
| New review actions: **Decide later** (keep pending), **Open CV PDF** | Faster review; "Reject" and "Skip" now say they are final |
| **Edit e-mail** opens Notepad (Windows) or `$EDITOR` | Real editing instead of a terminal text box. The edit is validated: one-line subject, minimum and maximum length, no leftover placeholders; warns when the title or company is no longer mentioned. |
| Review works without Gmail setup | Approve now, send later with `--send-queue` |
| Older pending applications are included in the `--apply` review | Nothing is forgotten |
| The search completion screen shows how many jobs have an application e-mail, and the next command | The flow continues naturally |
| Templates: `{greeting}` | Gives "Dear Hiring Team," when the company is unknown, and no " - Candidate" in the subject when the name is unknown. Accurate "over N years". More natural Indonesian wording. A clear error for a bad template placeholder. |
| Signature warning | Shown when the CV header has no name, phone or e-mail |
| `--applications` | Flags locked records (unknown delivery) and shows totals per status |
