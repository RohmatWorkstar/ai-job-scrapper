# AI Job Intelligence & Discovery System

A local-first Python CLI that reads your CV, builds an evidence-based candidate profile, searches many
public job sources, verifies what it finds, ranks every opportunity with a transparent explanation, and
writes a clickable **DOCX** report and **XLSX** workbook.

It is not a website and not a single-portal scraper. It runs on your machine. It sends data only to the
APIs you configure, and the CV goes out only with your consent and with contact details removed.

> **Principle:** maximise discovery, preserve evidence, verify facts, never fabricate.

---

## 1. Quick start (Windows 10/11)

```powershell
# 1. Create a virtual environment (Python 3.11 - 3.13 recommended)
py -3.12 -m venv .venv            # or: uv venv .venv --python 3.12
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional but recommended) add API keys
copy .env.example .env            # then edit .env

# 4. Put your CV (PDF, DOCX or TXT) into the cv\ folder

# 5. Run
python main.py
```

Linux/macOS are the same, except you activate the environment with `source .venv/bin/activate`.

**The app works without any API key.** It then uses deterministic CV analysis and searches only the
sources that need no credentials (see §4). Add keys to unlock web, portal, social media and community
search, and AI assistance.

---

## 2. What happens when you run `python main.py`

1. **Environment check**: Python version, configuration, CV folder, SQLite database and which keys are set.
2. **CV selection**: choose from the files in `cv/`. Scanned (image-only) PDFs are detected and explained.
3. **Consent**: if a Gemini key is set, you are asked before a redacted copy of the CV text is sent.
   E-mail addresses, phone numbers and links are removed first.
4. **Candidate profile**: title, experience (computed from employment dates), seniority estimate, skills
   grouped by type with CV evidence, industries, certifications, languages, suggested and adjacent positions.
   Skills the AI suggests but the CV does not prove are listed separately as *transferable*.
5. **Ten questions**: geography, employment type, positions, work arrangement, publication date, salary,
   seniority, sources, language and free-text preferences. **Press Enter to keep each default.**
   Your answers are saved; the app asks before it overwrites saved defaults.
6. **Summary**: choose *Confirm and start*, *Edit configuration* or *Cancel*.
7. **Live progress**: seven pipeline stages with real counters (queries per category, results, unique
   opportunities, pages checked, AI calls). **Press Ctrl+C once** to stop early. The partial results are
   still verified, ranked and written to the reports.
8. **Reports**: written to `reports/<YYYY-MM-DD_HHMMSS>/` (Asia/Jakarta time).

Example of the closing summary (the counts come from your real run):

```text
────────────────────────────── SEARCH COMPLETED ──────────────────────────────
  Total discoveries:                304
  Unique opportunities:             268
  Duplicates merged:                 36
  Highly relevant:                    7
  Relevant:                         139
  ...
Reports generated:
  [OK] reports\2026-11-22_214834\JOB_SEARCH_REPORT.docx
  [OK] reports\2026-11-22_214834\JOB_SEARCH_RESULTS.xlsx
Research completed at: 2026-11-22 21:50
Timezone: Asia/Jakarta
```

### Command-line options

| Option | Purpose |
|---|---|
| `--cv FILE` | Use this file from `cv/` and skip the selection prompt |
| `--defaults` | Non-interactive run: use saved answers (or the defaults) for every question |
| `--no-ai` | Do not call the AI provider in this run |
| `--max-queries N` | Override the paid web-search query budget for this run |
| `--check` | Check the configuration and test each source, then exit |
| `--history` | List previous search sessions |
| `--report SESSION_ID` | Rebuild the DOCX and XLSX for a stored session, offline |

---

## 3. Configuration

### `.env` (credentials, never committed)

| Variable | Needed for |
|---|---|
| `GEMINI_API_KEY` | AI CV interpretation, extraction help and semantic matching. Leave `AI_MODEL` empty to auto-select the newest stable Gemini Flash model through the official `models.list` API. |
| `SEARCH_PROVIDER=brave`, `SEARCH_API_KEY` | Brave Search API, which powers web, job-portal, social-media, community, careers-page and ATS-board discovery. |
| `SECONDARY_SEARCH_PROVIDER`, `SECONDARY_SEARCH_API_KEY` | A fallback provider used when the first one fails or hits its quota. |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | The official Reddit API (app-only OAuth). |
| `X_BEARER_TOKEN` | X API v2 recent search (requires a paid X tier). |

Keys never appear in the terminal, logs or reports. Logs also redact e-mail addresses and phone numbers.

### `config/settings.yaml`

Holds the defaults for every question, the budgets that cap API cost (`budget.*`) and the scoring
weights and thresholds (`matching.*`). It also holds the scam and closed-listing phrases, the domains that
are never fetched, concurrency and timeouts, per-host rate limits and cache lifetimes, and the list of
source connectors.

### `config/search_profiles.yaml`

Holds the hiring phrases (English and Indonesian), the job portals, social and community sites, and the
role families used for matching. It also lists the **ATS seed boards** (all checked to exist on
2026-11-22) and your own **company career pages**, plus country aliases. Add target companies here, for
example `greenhouse: [yourcompany]` or `company_career_pages: [{company: X, url: https://...}]`.

### `config/skills_taxonomy.yaml`

The technology list used to detect skills in CVs and job posts. Extend it freely.

---

## 4. Sources and their status

| Source | How it is accessed | Status |
|---|---|---|
| **Greenhouse** company boards | Official Job Board API | ✅ Fully implemented, no key needed |
| **Lever** company boards | Official Postings API | ✅ Fully implemented, no key needed |
| **Ashby** company boards | Official Job Posting API | ✅ Fully implemented, no key needed |
| **SmartRecruiters** company boards | Official Posting API | ✅ Fully implemented, no key needed |
| ATS board **auto-discovery** | Board names taken from search-result URLs, then crawled through the APIs above | ✅ Implemented, needs a search key |
| **Remotive, Remote OK, Arbeitnow, Jobicy, Himalayas** | Their public job APIs, with attribution kept and responses cached | ✅ Fully implemented, no key needed. Himalayas is often slow, and timeouts are reported. |
| **Hacker News "Who is hiring?"** | HN Search API (Algolia) | ✅ Fully implemented, no key needed |
| **Web search** (targeted hiring queries) | Brave Search API | 🔑 Implemented, needs `SEARCH_API_KEY` |
| **Job portals**: LinkedIn Jobs, JobStreet, Indeed, Glints, Kalibrr, Glassdoor, Wellfound, Tech in Asia, Dealls, We Work Remotely | Publicly indexed pages through `site:` search queries | 🔑 Implemented, needs a search key. These portals are not scraped directly. |
| **Social media**: LinkedIn posts, X, Facebook, Instagram, Threads | Publicly indexed posts through search | 🔑 Implemented, needs a search key. Only public, indexed content is used. |
| **X API** recent search | Official X API v2 | 🔑 Implemented, needs a paid `X_BEARER_TOKEN` |
| **Reddit** | Official Reddit API (OAuth) | 🔑 Implemented, needs Reddit app credentials |
| **Communities**: Reddit, Dev.to, HN, Kaskus | Publicly indexed pages through search | 🔑 Implemented, needs a search key |
| **Company career pages** | Direct fetch (robots.txt respected). JSON-LD `JobPosting` is extracted, otherwise links matching your roles. | ✅ For pages you list in the config. 🔑 For careers pages found by search. |
| **Workday** | No documented public API. Postings are only found through search. | ⚠️ Partially supported |
| **JavaScript-only career pages** | Optional Playwright renderer | ⚠️ Optional. Install with `pip install playwright` and `python -m playwright install chromium`, then set `extraction.use_browser_rendering: true`. |
| Google Custom Search API | n/a | ❌ Not implemented: closed to new customers and shutting down on 2027-01-01 |
| Bing Web Search API | n/a | ❌ Not implemented: retired |
| Private groups, logged-in feeds, CAPTCHA-protected pages | n/a | ❌ Not supported by design |

LinkedIn, Facebook, Instagram, X, Threads, Glassdoor and Indeed pages are **never fetched directly**,
because they sit behind login walls and their terms forbid it. Results from those sites stay *UNVERIFIED*
or *NEEDS_REVIEW* until you check them yourself.

---

## 5. How results are processed

1. **Query generation** builds queries from your target roles, skills, countries, work arrangement,
   employment type, language (Indonesian *lowongan* / *loker* phrasing included) and hiring phrases. The
   paid search budget is split across categories, and the queries rotate across platforms and countries so
   that a small budget still covers many of them.
2. **Prefiltering for high-volume boards.** ATS boards can list hundreds of unrelated roles, so the app
   keeps postings whose titles match your role families or skills. It drops postings whose *known*
   publication date falls outside your date window. Postings with an unknown date are always kept. Each
   source reports how many listings it scanned, kept and dropped.
3. **Extraction.** Pages are fetched within a budget, after SSRF and robots.txt checks. schema.org
   `JobPosting` JSON-LD, dates, apply links and closing notices are extracted. Optional AI extraction runs
   on unstructured snippets, and **every value it returns must appear in the source text**, otherwise the
   value is discarded.
4. **Normalisation.** Titles, companies, countries, currencies, employment types, work arrangements and
   dates are normalised. Unknown values stay `null` or `unknown`. The publication date, the social-post
   date and the discovery time are kept separately.
5. **Deduplication.** Entries are merged when they share an ATS ID, a canonical URL or an application URL,
   or when the company matches, the titles are nearly identical, the locations are compatible and the
   descriptions are similar. Every source link is kept under *Also found at*. Uncertain matches are kept
   apart and flagged as *possible duplicates*.
6. **Verification.** Each opportunity gets a status and a written reason:

   | Status | When |
   |---|---|
   | `VERIFIED_OPEN` | The posting is in the company's own live ATS listing, or on a company-owned page with structured posting data, no closing notice and an application link |
   | `LIKELY_OPEN` | It is in a live third-party feed, or on a page with structured data that cannot be fully confirmed |
   | `UNVERIFIED` | It is known only from a search snippet, or the page did not confirm it |
   | `NEEDS_REVIEW` | It is a social or community post (company authorisation not confirmed), or it may not be a job |
   | `CLOSED` | The page says the role is closed, or its `validThrough` date has passed |
   | `INACCESSIBLE` | The original page returns 404/410 or cannot be reached |
   | `POSSIBLE_SCAM` | It contains fee or payment warning signs. Anti-fraud disclaimers such as "we never ask for a fee" are ignored. |

   An HTTP 200 response alone never produces `VERIFIED_OPEN`.
7. **Matching.** Each opportunity gets a 0–100 *estimated relevance indicator*, not a probability of
   being hired. The default weights, which you can change, are: position 25, skills 25, experience 15,
   location and work arrangement 15, salary 10, freshness 10. Missing information gives a neutral value
   and a *To verify* note instead of a zero. Gates stop a high average from hiding a decisive mismatch,
   such as a remote role limited to another region, the wrong job family or a very different seniority.
   Results are grouped as HIGHLY RELEVANT, RELEVANT, POTENTIALLY RELEVANT, NEEDS MANUAL REVIEW or NOT
   RELEVANT. Nothing is dropped silently: NOT RELEVANT items stay in the XLSX and in the DOCX appendix.
   When AI is enabled, it adds a role-equivalence judgement and a short assessment for the top candidates.
8. **History.** Each run has its own session ID. Opportunities seen before are marked *seen in earlier
   searches* but are not hidden. The *Include previous jobs* option re-checks still-open jobs from earlier
   runs and adds them to the report.

---

## 6. Output files

```text
reports/2026-11-22_214834/
    JOB_SEARCH_REPORT.docx     # 9 sections + appendix, clickable links
    JOB_SEARCH_RESULTS.xlsx    # every opportunity, filters, frozen panes, clickable links
    search_metadata.json       # configuration, counts, per-source status, queries, limitations
    raw_discovery.json         # every raw discovery + every normalised opportunity
```

**DOCX sections:**
1. Executive Summary
2. Search Coverage, by country, source category, platform and per-source status. The report separates
   sources that were actually searched from sources that are only supported.
3. Highly Relevant
4. Relevant
5. Potentially Relevant
6. Social Media & Community
7. International, grouped by country with salaries in their original currency
8. Needs Verification / Closed / Suspicious
9. Source & Research Logs: the queries executed, errors and coverage limitations

**XLSX sheets:** Executive Summary, All Opportunities, Highly Relevant, Social Media Findings,
International Jobs, Needs Verification, Source Coverage and Queries Executed. Text taken from the web is
stored as plain text, never as formulas, so a scraped value cannot inject a formula.

Data is also stored in `data/database/job_intelligence.sqlite3`: profiles, sessions, queries, raw
discoveries, opportunities, source references, verification history, errors and report paths.

---

## 7. Security and compliance

- Credentials live only in `.env`, which is git-ignored. `.env.example` contains no secrets.
- Every outgoing URL is validated: only http/https, no embedded credentials, and every DNS answer must be
  a public IP address. Redirects are followed by hand and re-validated on each hop, and credentials are
  dropped on cross-host redirects.
- robots.txt is respected for every page fetch. Login-walled platforms are never fetched. CAPTCHAs,
  paywalls and anti-bot measures are never bypassed.
- Requests are throttled per host and concurrency is bounded. Timeouts and limited retries with
  exponential backoff apply. A 401/403 response is never retried.
- Only text responses (HTML, JSON, XML) are processed, with a size cap. Files are never downloaded or run.
- Web content is treated as untrusted data. AI prompts wrap it in explicit *untrusted data* markers, and
  AI output is validated against the source text.
- The CV goes only to the configured AI provider, only with your consent, and with contact details
  removed. Search providers receive only queries (role, skill and country terms), never the CV.

---

## 8. Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest                      # offline suite: 165 tests (1 live test skipped)
$env:RUN_LIVE_TESTS="1"; python -m pytest tests/integration/test_live_optin.py   # opt-in network test
```

The offline suite uses realistic fixtures served through a mock HTTP transport. It covers:

- CV parsing for PDF (multi-page and scanned), DOCX and TXT, and AI anti-fabrication rules
- configuration loading, question defaults and real Questionary prompts driven by simulated Enter and arrow keys
- query generation, HTML and JSON-LD extraction, normalisation, salary, date and timezone parsing
- SSRF protection, robots.txt, retries, size caps and the HTTP cache
- deduplication, verification, matching and scam detection
- the Brave, Greenhouse, Lever and Gemini adapters, including authentication and failure cases
- per-source failure isolation, DOCX hyperlinks, XLSX hyperlinks and formula-injection safety, the
  repository and history
- end-to-end pipelines with and without AI, report regeneration and interruption

---

## 9. Extending

**Add a source:** subclass `src.sources.base.BaseJobSource`, implement `search()` (and override `run()`
or `health_check()` if needed), then add `"src.sources.<module>:<Class>"` to `discovery.connectors` in
`settings.yaml`. The discovery engine needs no change.

**Add a search provider:** subclass `SearchProvider` in `src/sources/search_engine/providers.py` and
register it in `PROVIDERS`.

**Add an AI provider:** implement `src.ai.base.AIProvider` and register it in
`src/ai/provider_manager.py`.

---

## 10. Known limitations

- Coverage is limited to the sources listed in §4. The app does not search "the whole internet".
- Without a search API key, portals, social media, communities and general web results are **not searched**.
  The report says this explicitly.
- Search-engine date filters reflect when a page was indexed, so some older postings can appear. Those
  dates are labelled as approximate.
- The 7-day default window excludes many older ATS postings that are still open. Choose *Last 30 days*
  or *All available dates* for more of them.
- Remotive delays its API data by 24 hours under its terms.
- Social and community posts cannot prove that the company authorised them. They always need your review.
- Scam detection is a heuristic based on warning phrases. It is not a guarantee.
- PDFs that are only scanned images need OCR first. The app detects them and explains the problem; it does not OCR them.
- The code is written against the documented Brave and Gemini APIs and tested with fixtures. It has not
  been run against live Brave or Gemini accounts in this project, so run `python main.py --check` after
  you add your keys.

---

## 11. Application automation (e-mail applications)

Applies by e-mail to scraped jobs whose posting contains an application address. Every e-mail
needs your approval, and sending goes through conservative Gmail safety limits. Full details,
the Gmail limit research and the setup guide are in
[FINAL_DEVELOPMENT_REPORT.md](FINAL_DEVELOPMENT_REPORT.md).

```powershell
python main.py                                 # 1. search jobs (unchanged)
python main.py --setup-check                   # what is set up / what you still have to do manually
python main.py --apply basic                   # 2a. original CV PDF + template e-mail, review each one
python main.py --apply ai                      # 2b. AI job analysis, match score, tailored CV PDF + e-mail
python main.py --review                        # review applications still waiting for approval
python main.py --send-queue                    # send approved applications within the limits
python main.py --applications                  # application history
python main.py --sending-status                # counters, limits, pause / emergency-stop state
python main.py --emergency-stop                # block all sending immediately (--resume-sending to clear)
python main.py --gmail-auth                    # one-time Gmail OAuth consent (gmail.send scope only)
```

**Dry run is the default.** `applications.sending.dry_run: true` writes `.eml` files to
`data/outbox/` and never contacts Gmail. A dry-run "send" is a rehearsal: the application goes back
to review, and you approve it again (with dry run off) to send it for real.

---

## Project layout

```text
main.py                    entry point
config/                    settings.yaml, search_profiles.yaml, skills_taxonomy.yaml
cv/                        put CVs here (git-ignored)
src/cli/                   application flow, questionnaire, Rich display
src/core/                  configuration, models, orchestrator, pipeline state, exceptions
src/cv/                    parser, deterministic analyzer, AI-validated profile builder
src/discovery/             query generator, source registry, discovery manager
src/sources/               search_engine/, job_portals/, ats_platforms/, company_careers/, social_media/, communities/
src/extraction/            HTML/JSON-LD extractor, browser renderer, content parser, normaliser
src/intelligence/          relevance prefilter, verification, evidence, deduplication, matching
src/ai/                    provider interface, Gemini REST provider, prompts, manager (budget + cache)
src/storage/               SQLite schema, repository, history & preferences
src/reporting/             report model, DOCX and XLSX generators
src/applications/          e-mail application automation (Basic/AI modes, Gmail, sending limits, tracker)
src/utils/                 safe HTTP client, validators, dates, logging, retry
tests/                     unit/, integration/, fixtures/, test_pipeline.py
data/                      database, cache, logs (created at runtime, git-ignored)
reports/                   generated reports (git-ignored)
```
