# AI Job Intelligence & Discovery System

A local-first Python CLI that reads your CV, builds an evidence-based candidate profile, searches many
public job sources, verifies what it finds, ranks every opportunity with a transparent explanation, and
writes a clickable **DOCX** report and **XLSX** workbook.

It is not a website and not a single-portal scraper. It runs on your machine. It sends data only to the
APIs you configure, and the CV goes out only with your consent and with contact details removed.

> **Principle:** maximise discovery, preserve evidence, verify facts, never fabricate.

---

## Panduan cepat (Bahasa Indonesia)

Ringkasan cara memakai project ini sehari-hari. Detail teknis ada di bagian berbahasa Inggris di bawah
dan di [FINAL_DEVELOPMENT_REPORT.md](FINAL_DEVELOPMENT_REPORT.md).

### A. Setup pertama kali (atau di komputer baru)

```powershell
cd "E:\NEW PROJECT 2027\Job-Scrapper"
py -3.12 -m venv .venv                 # atau: uv venv .venv --python 3.12
.venv\Scripts\activate
pip install -r requirements.txt        # tanpa pip: uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy .env.example .env                 # lalu isi .env (lihat tabel di bawah)
```

1. Taruh CV di folder `cv\`. **Wajib ada versi PDF**, karena Basic mode melampirkan PDF asli.
   CV tidak pernah diubah oleh aplikasi.
2. Isi `.env`. File ini tidak ikut ke git, jadi jangan pernah membagikan isinya.

   | Variabel | Fungsi | Wajib? |
   |---|---|---|
   | `GMAIL_SENDER_ADDRESS` | Alamat Gmail pengirim | Ya, untuk kirim sungguhan |
   | `GMAIL_APP_PASSWORD` | App Password Gmail (buat di <https://myaccount.google.com/apppasswords>, butuh 2-Step Verification) | Ya, untuk kirim sungguhan |
   | `APPLICATION_TEST_RECIPIENT` | **Mode tes:** semua e-mail dibelokkan ke alamat ini, perusahaan tidak menerima apa pun | Isi hanya saat tes |
   | `SEARCH_API_KEY` | Brave Search, untuk portal Indonesia, komunitas, dan postingan rekruter. **Tanpa ini hampir tidak ada lowongan yang bisa dilamar lewat e-mail.** | Sangat disarankan |
   | `GEMINI_API_KEY` | AI untuk analisis lowongan dan CV yang disesuaikan per lowongan | Opsional |
   | `APPLICANT_NAME` / `APPLICANT_PHONE` / `APPLICANT_LINKS` | Menimpa data kontak dari CV | Opsional |

3. Cek semuanya dengan perintah di bawah. Setiap baris `TODO` menyertakan cara memperbaikinya.
   ```powershell
   python main.py --setup-check
   ```

### B. Rutinitas harian

```powershell
.venv\Scripts\activate

# Pintu utama interaktif (Hub): Search, Browse/Filter Jobs, Prepare, Review, Send Queue, Pipeline & Setup
python main.py

# Atau jalankan langsung via flag CLI (kompatibel penuh dengan skrip & Task Scheduler):
python main.py --search                       # langsung mulai pencarian lowongan
python main.py --apply web                    # Web/ATS Assist: CV PDF tailored + Cover Note + buka URL + tandai Applied
python main.py --apply ai --channel all       # E-mail untuk job ber-email, otomatis fallback ke Web/ATS Assist
python main.py --apply basic                  # CV PDF asli + e-mail template (cepat)
python main.py --apply ai --all-recent 14     # proses lowongan lintas sesi dalam 14 hari terakhir
python main.py --review                       # review lamaran pending (Email & Web/ATS)
python main.py --send-queue                   # kirim antrean e-mail yang sudah di-approve
python main.py --applications                 # riwayat lamaran + funnel pipeline (opsional: --status INTERVIEW)
python main.py --update-status <ID> interview --note "HR call Senin"
python main.py --sending-status               # kuota hari ini, jeda, status pause/stop
```

Semua lowongan — baik yang lewat **Web/ATS** (Greenhouse, Lever, Ashby, SmartRecruiters, We Work Remotely, Working Nomads) maupun yang lewat **E-mail** — kini bisa memakai **tailored ATS CV PDF**, **cover note**, dan **Application Tracker** hingga tahap *Interview*, *Offer*, *Employer Rejected*, atau *Withdrawn*.

### C. Mode tes vs kirim sungguhan

| Keadaan | `dry_run` di `config/settings.yaml` | `APPLICATION_TEST_RECIPIENT` di `.env` | Hasil |
|---|---|---|---|
| Latihan tanpa internet | `true` | (bebas) | File `.eml` di `data\outbox\`, tidak ada yang terkirim |
| Tes kirim ke inbox sendiri | `false` | `alamat-tes@gmail.com` | Semua e-mail dikirim ke alamat tes dengan subjek `[TEST for ...]` |
| **Melamar sungguhan** | `false` | **kosong** | E-mail dikirim ke perusahaan |

Setelah dry run atau tes, lamaran kembali ke status review. Untuk mengirim sungguhan, approve lagi
setelah mode diganti.

### D. Batas pengiriman (supaya akun Gmail aman)

Setting saat ini (opsi A, ±100 lamaran dalam ±3 hari kerja), di `config/settings.yaml` →
`applications.sending`:

| Setting | Nilai |
|---|---|
| `max_per_day` | 35 |
| `max_per_hour` | 8 |
| `min_interval_seconds` | 120 |
| Jam kirim | 08:00–18:00 WIB, Senin–Jumat |
| `recipient_cooldown_days` | 30: **satu alamat penerima hanya menerima satu lamaran** dalam 30 hari |

- Naikkan bertahap. Nilai di atas 50 per hari memunculkan peringatan, dan nilai di atas 250 ditolak sistem.
- Kalau Gmail menolak karena limit, pengiriman otomatis berhenti 24 jam dan tidak dicoba ulang.
- **Berhenti darurat:** jalankan `python main.py --emergency-stop` (atau buat file `data\STOP_SENDING`).
  Untuk melanjutkan: `python main.py --resume-sending`.

**Kirim otomatis sepanjang hari** lewat Windows Task Scheduler. Jalankan sekali di PowerShell:

```powershell
schtasks /Create /TN "JobScrapper-SendQueue" /SC MINUTE /MO 30 /ST 08:00 /ET 18:00 /D MON,TUE,WED,THU,FRI `
  /TR "cmd /c cd /d \"E:\NEW PROJECT 2027\Job-Scrapper\" && .venv\Scripts\python.exe main.py --send-queue --defaults >> data\logs\send_queue.log 2>&1"
# hapus: schtasks /Delete /TN "JobScrapper-SendQueue" /F
```

### E. Masalah umum

| Pesan / gejala | Arti dan solusi |
|---|---|
| `0 opportunity(ies) ... by e-mail` / "None of these postings accepts applications by e-mail" | Sumber gratis hanya berisi board ATS yang memakai form web. Tambahkan `SEARCH_API_KEY`, atau lamar lewat link di XLSX. |
| `Outside the sending window` | Di luar jam 08–18 hari kerja. Tunggu, atau ubah `sending_window`. |
| `Hourly limit reached` / `Daily limit reached` | Batas kirim tercapai. Sisa antrean dikirim pada `--send-queue` berikutnya. |
| `Sending paused: ...` | Gmail menolak atau ada beberapa kegagalan beruntun. Baca alasannya, lalu `python main.py --resume-sending`. |
| `535` / `App Password rejected` | App Password salah atau dicabut. Buat yang baru dan perbarui `.env`. |
| `LOCKED - delivery unknown` di `--applications` | Koneksi putus saat mengirim. Cek folder *Sent* di Gmail. Kalau **tidak** terkirim: `python main.py --requeue <ID>`. |
| `already has an application from you` | Alamat itu sudah menerima atau sedang menunggu lamaran dari Anda (aturan satu alamat satu lamaran). |
| Data di CV terbaca salah | Jalankan `python main.py --apply ai --limit 1 --dry-run` lalu pilih *Open CV PDF*. Pastikan CV memakai heading standar (PROFILE, EXPERIENCE, PROJECTS, SKILLS, EDUCATION). |

### F. File dan folder penting

| Lokasi | Isi |
|---|---|
| `cv\` | CV asli (tidak pernah diubah) |
| `.env` | Credential (rahasia, tidak ikut ke git) |
| `config\settings.yaml` | Semua setting: filter lowongan (`applications.eligibility`), batas kirim, mode |
| `config\email_templates.yaml` | Teks e-mail (EN/ID); boleh diedit |
| `reports\` | Laporan hasil pencarian (DOCX/XLSX) |
| `data\generated_cvs\` | CV PDF yang disesuaikan per lowongan (AI mode) |
| `data\outbox\` | E-mail hasil dry run (`.eml`) |
| `data\database\job_intelligence.sqlite3` | Riwayat lowongan dan lamaran (backup sesekali) |
| `data\logs\` | Log aplikasi (password dan alamat e-mail disensor) |

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

# 4. Put your CV (PDF recommended; DOCX, TXT, or DOC with antiword/LibreOffice) into the cv\ folder

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
| *(no action flags, interactive)* | Launch the **Interactive Hub** (Search, Browse Jobs, Prepare, Review, Send Queue, Pipeline, Setup/Health) |
| `--search` | Jump directly into the job search flow without opening the Hub |
| `--cv FILE` | Use this file from `cv/` and skip the selection prompt |
| `--defaults` | Non-interactive run: use saved answers (or the defaults) for every question |
| `--no-ai` | Do not call the AI provider in this run (propagated across CV, extraction, matching and tailoring) |
| `--ai-consent` | Explicitly opt in to Gemini CV analysis in non-interactive (`--defaults`) mode |
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
| **Remotive, Remote OK, Arbeitnow, Jobicy, Himalayas, Working Nomads, We Work Remotely (RSS)** | Public job APIs & RSS feeds, with attribution kept and responses cached | ✅ Fully implemented, no key needed. Himalayas is often slow, and timeouts are reported. |
| **Hacker News "Who is hiring?"** | HN Search API (Algolia) with monthly-aware window | ✅ Fully implemented, no key needed |
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
python -m pytest                      # offline suite: 198 tests (1 live test skipped)
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
  you add your keys. Gmail sending over SMTP (App Password) **was** tested live on 2026-11-23.
- Postings from the free sources (ATS boards, remote feeds) almost never accept applications by e-mail;
  e-mail applications mostly come from portals, community and social posts, which need a search key.

---

## 11. Multi-channel application automation (E-mail & Web/ATS Assist)

Supports both direct **e-mail applications** (approval-gated with Gmail safety limits) and **Web/ATS Assist**
(tailored ATS PDF CV + paste-ready cover note + browser launch + manual `APPLIED_WEB` confirmation) plus full
post-apply lifecycle tracking (`INTERVIEW`, `OFFER`, `EMPLOYER_REJECTED`, `WITHDRAWN`).

```powershell
python main.py                                 # Interactive Hub (recommended daily entry point)
python main.py --setup-check                   # what is set up / what you still have to do manually
python main.py --apply web                     # Web/ATS Assist: tailored CV PDF + cover note for web/ATS jobs
python main.py --apply ai --channel all        # e-mail when available, otherwise Web/ATS Assist
python main.py --apply basic                   # original CV PDF + template e-mail, review each one
python main.py --apply ai                      # AI job analysis, match score, tailored CV PDF + e-mail
python main.py --apply web --all-recent 14     # prepare from deduplicated jobs across the last 14 days
python main.py --review                        # review pending e-mail and Web/ATS applications
python main.py --send-queue                    # send approved e-mail applications within the limits
python main.py --applications [--status X]     # application history & pipeline funnel
python main.py --update-status <ID> interview --note "Technical screen"
python main.py --sending-status                # counters, limits, pause / emergency-stop state
python main.py --emergency-stop                # block all sending immediately (--resume-sending to clear)
python main.py --requeue <APPLICATION_ID>      # FAILED / unknown delivery -> back to review
python main.py --gmail-auth                    # Gmail API OAuth (gmail.send scope only), if you use it
```

Useful flags: `--session ID` (jobs from a specific search), `--limit N`, `--dry-run`,
`--allow-duplicate` (asks for confirmation), `--auto` (automatic mode, also needs
`mode: automatic` and `automatic_enabled: true`).

| Topic | Behaviour |
|---|---|
| Gmail connection | `GMAIL_APP_PASSWORD` set → `smtp.gmail.com` (no Google Cloud project needed). Otherwise the Gmail API with OAuth (`--gmail-auth`). |
| Dry run | `applications.sending.dry_run: true` writes `.eml` files to `data/outbox/` and never contacts Gmail |
| Test recipient | `APPLICATION_TEST_RECIPIENT` sends for real, but only to that address. The employer receives nothing. |
| Rehearsals | After a dry run or test send, the application returns to review. Approve it again in live mode to send it for real. |
| Duplicates | One application per company + position, and **one per recipient address** within `recipient_cooldown_days` (30) |
| Recipient extraction | Only addresses with an explicit "send your CV to …" instruction, or a recruiting inbox. Accommodation, accessibility, questions and fraud-report addresses are ignored. |
| E-mail text | Plain and factual. The current role comes from the CV dates; one real CV bullet relevant to the job; a skill is called "used in work" only when the CV shows it in experience or projects. |
| Limits | Option A: 35/day, 8/hour, 120 s apart, 08–18 on weekdays; automatic pause on Gmail limit errors; emergency stop |

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
