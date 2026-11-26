# AI Job Intelligence & Application Automation

Aplikasi CLI lokal berbasis Python untuk mencari lowongan kerja dari banyak sumber publik, menyaring kecocokan dengan CV Anda, membuat laporan **DOCX** dan **XLSX**, serta membantu melamar lewat **Email** maupun **Web/ATS** dengan kontrol penuh di tangan Anda.

Semua berjalan di komputer Anda sendiri. CV asli di folder `cv/` tidak pernah diubah, dan data kontak Anda dihapus terlebih dahulu sebelum teks CV dikirim ke AI (itu pun hanya jika Anda mengaktifkan `GEMINI_API_KEY` dan menyetujui consent).

> **Prinsip utama:** verifikasi fakta, jaga bukti sumber, jangan pernah mengarang pengalaman atau skill.

---

## Daftar Isi

1. [Instalasi & Setup Awal](#1-instalasi--setup-awal)
2. [Setup Email Pengirim & Mode Test vs Live](#2-setup-email-pengirim--mode-test-vs-live)
3. [Penjelasan Semua Mode](#3-penjelasan-semua-mode)
4. [Cara Pakai Sehari-hari](#4-cara-pakai-sehari-hari)
5. [Sumber Lowongan & Kualitas Email](#5-sumber-lowongan--kualitas-email)
6. [Hasil Output & Laporan](#6-hasil-output--laporan)
7. [Keamanan, Batas Kirim & Tombol Darurat](#7-keamanan-batas-kirim--tombol-darurat)
8. [Masalah Umum & Solusi](#8-masalah-umum--solusi)
9. [Struktur Project & Testing](#9-struktur-project--testing)

---

## 1. Instalasi & Setup Awal

Butuh **Python 3.11 – 3.13** (disarankan Python 3.12).

### Windows (PowerShell)

```powershell
# 1. Buat virtual environment dan aktifkan
py -3.12 -m venv .venv                 # atau: uv venv .venv --python 3.12
.venv\Scripts\activate

# 2. Install dependency
pip install -r requirements.txt

# 3. Buat file konfigurasi rahasia (.env) dari contoh
copy .env.example .env
```

*(Untuk Linux/macOS, aktifkan environment dengan `source .venv/bin/activate` dan salin `.env.example` dengan `cp .env.example .env`.)*

### Langkah Wajib Sebelum Jalan

1. **Masukkan CV Anda ke folder `cv/`**  
   Disarankan format **PDF** berbasis teks (bukan hasil scan gambar). Format DOCX, TXT, dan DOC (butuh LibreOffice/antiword) juga didukung, tetapi Basic mode membutuhkan PDF.
2. **Isi file `.env`** (file ini di-ignore Git — jangan pernah di-commit atau dibagikan):

   | Variabel di `.env` | Fungsi | Wajib? |
   |---|---|---|
   | `GMAIL_SENDER_ADDRESS` | Alamat Gmail pengirim (`From`) | Wajib untuk kirim email |
   | `GMAIL_APP_PASSWORD` | App Password 16 karakter dari [Google App Passwords](https://myaccount.google.com/apppasswords) (butuh 2-Step Verification) | Wajib jika pakai SMTP (paling mudah) |
   | `APPLICATION_TEST_RECIPIENT` | **Mode tes:** belokkan semua email ke inbox pribadi Anda; perusahaan asli menerima 0 email | Isi saat tes, **kosongkan saat live** |
   | `SEARCH_API_KEY` | Brave Search API (`SEARCH_PROVIDER=brave`) untuk portal Indonesia, sosial media, dan lowongan email | Sangat disarankan |
   | `GEMINI_API_KEY` | AI Gemini untuk analisis CV/lowongan, tailoring CV, dan penulisan email bebas ber-fact-check | Opsional (tanpa ini tetap jalan mulus secara deterministik) |
   | `APPLICANT_NAME`, `APPLICANT_EMAIL`, `APPLICANT_PHONE`, `APPLICANT_LINKS` | Menimpa data kontak/link yang terbaca dari header CV | Opsional |

3. **Cek kesiapan sistem:**
   ```powershell
   python main.py --setup-check
   ```
   Setiap item yang masih `TODO` akan langsung menampilkan instruksi perbaikannya.

---

## 2. Setup Email Pengirim & Mode Test vs Live

### A. Mengatur Email Pengirim (`From`)

Buka file `.env` dan isi:

```env
GMAIL_SENDER_ADDRESS=nama.profesional@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

- **Tips HR:** Gunakan akun Gmail profesional yang **sama** dengan alamat email di CV Anda agar pengirim (`From`), `Reply-To`, dan signature konsisten.
- **Alternatif OAuth (tanpa App Password):** Kosongkan `GMAIL_APP_PASSWORD`, simpan file OAuth client JSON dari Google Cloud Console ke `data/secrets/gmail_client_secret.json`, lalu jalankan:
  ```powershell
  python main.py --gmail-auth
  ```
  OAuth hanya meminta izin kirim (`gmail.send`), tidak membuka akses penuh akun Gmail.

### B. Menyalakan / Mematikan Mode Email Tester

Sistem memiliki 3 tingkat keamanan pengiriman yang dikontrol dari `config/settings.yaml` (`applications.sending`) dan `.env`:

| Kondisi | `dry_run` di `config/settings.yaml` | `APPLICATION_TEST_RECIPIENT` di `.env` | Apa yang Terjadi |
|---|---|---|---|
| **Latihan Offline (Dry Run)** | `true` *(default)* | *(bebas)* | Hanya menyimpan file `.eml` ke `data/outbox/`. Gmail tidak dihubungi. |
| **Tes Kirim ke Inbox Sendiri (ON)** | `false` | `email.test.anda@gmail.com` | Email benar-benar dikirim lewat Gmail, tetapi **100% dibelokkan ke email test Anda**. Perusahaan asli tidak menerima apa pun. |
| **Kirim Sungguhan ke Perusahaan (OFF)** | `false` | **kosong** *(dan `test_recipient: ""` di `settings.yaml`)* | Email dikirim **langsung ke alamat HR perusahaan**. |

**Pengaturan tampilan Subject saat Mode Test:**
- Di `config/settings.yaml`: `test_tag_subject: false` *(default)* → Subject tampil natural seperti email asli tanpa kata `[TEST]` (alamat tujuan perusahaan asli tetap dicatat di header `X-Original-To`).
- Ubah ke `test_tag_subject: true` (atau set `APPLICATION_TEST_TAG_SUBJECT=true` di `.env`) jika Anda ingin Subject diberi awalan `[TEST for hr@perusahaan.com]`.

> Setelah Dry Run atau Test Send selesai, status lamaran otomatis kembali ke `PENDING_APPROVAL` agar tetap bisa Anda approve dan kirim sungguhan nanti.

---

## 3. Penjelasan Semua Mode

### A. Mode Persiapan Lamaran (`--apply`)

| Mode | Fungsi | Kapan Dipakai |
|---|---|---|
| **`basic`** | Melampirkan **CV PDF asli** Anda + email yang disesuaikan dengan posisi target. Cepat dan tidak membuat PDF baru. | Saat CV asli sudah cocok dengan lowongan dan ingin proses cepat. |
| **`ai`** | Menganalisis JD, menghitung skor kecocokan, membuat **Custom ATS PDF CV** khusus per perusahaan (skill & pengalaman relevan diurutkan ke atas), dan menyusun email khusus role tersebut. | Saat ingin CV dan email tampil paling relevan untuk tiap posisi. |
| **`web`** | **Web/ATS Assist** untuk lowongan yang melamarnya lewat form website (Greenhouse, Lever, Ashby, WWR, dll.). Menyiapkan Custom CV PDF + *cover note* siap tempel + membuka link lamaran. | Untuk mayoritas lowongan modern yang tidak mencantumkan alamat email. |

### B. Mode Channel (`--channel`)

Digunakan bersama `--apply basic` atau `--apply ai`:
- `--channel email` *(default)* — hanya memproses lowongan yang memiliki email HR valid.
- `--channel web` — hanya memproses lowongan via form Web/ATS.
- `--channel all` — **mode campuran**: lowongan ber-email diproses sebagai Email, sisanya otomatis dibuatkan paket Web/ATS Assist.

### C. Mode Pengiriman (`applications.sending.mode` di `config/settings.yaml`)

- **`manual`** *(default, paling aman)* — setiap lamaran berhenti di `PENDING_APPROVAL`. Tidak ada yang terkirim sebelum Anda review dan approve.
- **`scheduled`** — mengirim antrean `APPROVED` bertahap mengikuti jam kerja (`sending_window`), kuota per jam/hari, dan jeda antar email.
- **`automatic`** — auto-approve dan kirim untuk skor ≥ `automatic_min_match_score` (non-freemail). Demi keamanan, mode ini hanya aktif jika **ketiganya** dinyalakan bersamaan: `mode: automatic`, `automatic_enabled: true`, dan flag CLI `--auto`.

---

## 4. Cara Pakai Sehari-hari

### Cara Termudah: Interactive Hub

Cukup jalankan tanpa flag:

```powershell
python main.py
```

Dari satu menu ini Anda bisa langsung:
- Melihat badge status aktif (`DRY RUN` / `TEST -> ...` / `LIVE`) dan ringkasan pipeline lamaran.
- Mencari lowongan baru, menyaring hasil lowongan (by kategori, channel, negara, kata kunci).
- Menyiapkan lamaran (Basic / AI / Web Assist), review & edit email sebelum approve.
- Mengirim antrean email, membuka form Web/ATS dan menandai `APPLIED_WEB`, serta update status ke `INTERVIEW` / `OFFER` / `EMPLOYER_REJECTED` / `WITHDRAWN`.
- Mengecek kesehatan sistem (`setup-check`) dan membuat ulang laporan DOCX/XLSX.

### Cara Cepat via Command Line (CLI)

```powershell
# 1. Cari lowongan baru (tekan Enter untuk pakai default/preferensi tersimpan)
python main.py --search
# Tambah --defaults untuk jalan langsung tanpa pertanyaan, atau --no-ai untuk tanpa Gemini

# 2. Siapkan lamaran (Email + Web/ATS sekaligus)
python main.py --apply ai --channel all --limit 10
# Atau ambil dari gabungan seluruh sesi 14 hari terakhir:
python main.py --apply ai --channel all --all-recent 14

# 3. Review, edit teks/CV, dan approve lamaran
python main.py --review

# 4. Kirim antrean email yang sudah di-approve
python main.py --send-queue
# Tambah --dry-run kapan saja jika hanya ingin tes ke data/outbox/

# 5. Pantau & update tahap lamaran
python main.py --applications
python main.py --applications --status INTERVIEW
python main.py --update-status <APP_ID> interview --note "User interview Kamis jam 10"

# 6. Utilitas lain
python main.py --history                      # lihat daftar sesi pencarian
python main.py --report <SESSION_ID>          # buat ulang laporan DOCX/XLSX offline
python main.py --check                        # cek config dan koneksi sumber lowongan
python main.py --sending-status               # cek kuota kirim hari ini & status pause/stop
```

---

## 5. Sumber Lowongan & Kualitas Email

### Sumber Lowongan yang Didukung

| Sumber | Butuh API Key? | Catatan |
|---|---|---|
| **Greenhouse, Lever, Ashby, SmartRecruiters** | Tidak | API resmi board perusahaan. Jika negara menyertakan Indonesia, board SEA/Indonesia (Xendit, Flip, Kargo, Agoda, Wise, dll.) diprioritaskan lebih dulu. |
| **Remotive, Remote OK, Arbeitnow, Jobicy, Himalayas, Working Nomads, We Work Remotely (RSS)** | Tidak | Feed lowongan publik (hasil di-cache agar sopan terhadap rate limit). |
| **Hacker News ("Who is hiring?")** | Tidak | Menggunakan Algolia HN API dengan window bulanan otomatis (diperpanjang ke 35 hari saat filter ≥7 hari agar thread awal bulan tidak kosong di pertengahan/akhir bulan). |
| **Company Career Pages** | Tidak (untuk daftar di config) | Mengambil JSON-LD `JobPosting` secara sopan (mematuhi `robots.txt`). |
| **Web Search, Portal (Glints, Kalibrr, JobStreet, Tech in Asia, Dealls, LinkedIn Jobs, dll.), Sosial Media & Komunitas** | Ya (`SEARCH_API_KEY`) | Menggunakan Brave Search (`site:` query publik, tidak pernah scrape halaman login-wall secara langsung). |
| **Reddit API & X API** | Ya (kredensial resmi masing-masing) | Opsional. |

*Untuk halaman karir yang 100% JavaScript, Anda bisa mengaktifkan Playwright secara opsional (`pip install playwright && python -m playwright install chromium`, lalu set `extraction.use_browser_rendering: true` di `config/settings.yaml`).*

### Cara Kerja Penulisan Email Lamaran

- **Disesuaikan per kategori posisi:** Sistem mendeteksi fokus lowongan (`Fullstack`, `Backend`, `Frontend`, `Software Engineer`, atau `AI Integration`) dari judul, skill wajib, dan deskripsi pekerjaan.
- **Berbasis fakta CV:** Memilih 3–4 skill paling relevan dan 1–2 pencapaian nyata yang paling cocok (mempertahankan angka dampak seperti penghematan biaya atau penurunan bug), tanpa membuang-buang ruang dengan nama perusahaan lama secara otomatis.
- **Membedakan pengalaman kerja dan proyek:** Pengalaman profesional disebut jelas sebagai pengalaman kerja, sedangkan proyek disebut sebagai proyek.
- **Bahasa otomatis (`auto`):** Bahasa Inggris profesional untuk lowongan berbahasa Inggris, dan Bahasa Indonesia natural untuk lowongan Indonesia.
- **Anti-halusinasi:** Saat `GEMINI_API_KEY` aktif, output AI tetap diperiksa oleh `src/applications/fact_check.py`. Jika AI mengarang skill, angka, atau memakai klise berlebihan, sistem otomatis memakai versi deterministik yang terjamin faktual.
- **Link & kontak rapi:** Signature selalu menampilkan kontak valid dan tautan berlabel (`Portfolio:`, `GitHub:`, `LinkedIn:`).

---

## 6. Hasil Output & Laporan

Setiap sesi pencarian menyimpan hasil di `reports/<YYYY-MM-DD_HHMMSS>/`:

- **`JOB_SEARCH_REPORT.docx`** — Laporan rapi 9 bagian dengan tautan yang bisa diklik (`mailto:` untuk email, URL untuk Web/ATS) beserta status lamaran.
- **`JOB_SEARCH_RESULTS.xlsx`** — Workbook lengkap dengan filter & link aktif, mencakup sheet:
  *Executive Summary*, *All Opportunities* (lengkap dengan kolom `Apply Channel`, `Application Email`, `Application Status`), *Highly Relevant*, *Social Media Findings*, *International Jobs*, *Needs Verification*, ***Application Tracker***, *Source Coverage*, dan *Queries Executed*.
- **`search_metadata.json`** & **`raw_discovery.json`** — Log query, cakupan sumber, dan data mentah.
- **`data/generated_cvs/`** — CV PDF ATS-friendly hasil tailoring untuk AI mode / Web Assist.

---

## 7. Keamanan, Batas Kirim & Tombol Darurat

Pengaturan default di `config/settings.yaml` (`applications.sending`) dirancang agar akun Gmail tetap aman:

| Pengaturan | Default | Arti |
|---|---|---|
| `max_per_day` | `35` | Maksimal 35 email per 24 jam (di atas 50 muncul peringatan; di atas 250 ditolak sistem) |
| `max_per_hour` | `8` | Maksimal 8 email per jam |
| `min_interval_seconds` | `120` | Jeda minimal 2 menit antar email |
| `sending_window` | `08:00–18:00` (Senin–Jumat) | Hanya mengirim di jam kerja |
| `recipient_cooldown_days` | `30` | Satu alamat penerima (termasuk variasi `+tag` Gmail) hanya menerima 1 lamaran dalam 30 hari |

- **Auto-pause:** Jika Gmail mengembalikan error limit/kuota, pengiriman berhenti otomatis selama 24 jam tanpa retry berulang.
- **Tombol darurat:**
  ```powershell
  python main.py --emergency-stop    # hentikan semua pengiriman seketika (atau buat file data\STOP_SENDING)
  python main.py --resume-sending    # aktifkan kembali setelah masalah selesai
  ```
- **Jadwal kirim otomatis di Windows (opsional):**
  ```powershell
  schtasks /Create /TN "JobScrapper-SendQueue" /SC MINUTE /MO 30 /ST 08:00 /ET 18:00 /D MON,TUE,WED,THU,FRI `
    /TR "cmd /c cd /d \"E:\NEW PROJECT 2027\Job-Scrapper\" && .venv\Scripts\python.exe main.py --send-queue --defaults >> data\logs\send_queue.log 2>&1"
  # Untuk menghapus jadwal:
  # schtasks /Delete /TN "JobScrapper-SendQueue" /F
  ```
- **Perlindungan teknis:** Proteksi SSRF (memblokir IP privat/lokal termasuk tiap redirect), kepatuhan `robots.txt`, pencegahan formula injection di Excel, penyensoran secret & PII di log, serta tidak pernah menembus CAPTCHA/login-wall.

---

## 8. Masalah Umum & Solusi

| Gejala / Pesan | Penyebab & Solusi |
|---|---|
| `0 opportunity(ies) ... by e-mail` | Sumber gratis mayoritas memakai form Web/ATS. Gunakan `python main.py --apply web` atau `--apply ai --channel all`, atau tambahkan `SEARCH_API_KEY` di `.env` untuk mencari lowongan email. |
| `Outside the sending window` | Anda mengirim di luar jam 08:00–18:00 Senin–Jumat. Tunggu jam kerja atau sesuaikan `sending_window` di `config/settings.yaml`. |
| `Hourly limit reached` / `Daily limit reached` | Batas aman tercapai. Sisa antrean akan terkirim pada `--send-queue` berikutnya. |
| `535` / `App Password rejected` | `GMAIL_APP_PASSWORD` salah atau sudah dicabut. Buat App Password baru di akun Google dan perbarui `.env`. |
| `LOCKED - delivery unknown` | Koneksi terputus tepat saat mengirim. Cek folder **Sent** di Gmail terlebih dahulu; jika benar-benar **belum** terkirim, jalankan `python main.py --requeue <APP_ID>`. |
| `already has an application from you` | Alamat email HR tersebut sudah menerima/mengantre lamaran Anda dalam 30 hari terakhir (perlindungan anti-duplikat). |
| Hasil CV PDF terlihat kurang rapi | Jalankan `python main.py --apply ai --limit 1 --dry-run` lalu cek PDF di `data/generated_cvs/`. Pastikan CV asli memakai heading standar (PROFILE, EXPERIENCE, PROJECTS, SKILLS, EDUCATION). |

---

## 9. Struktur Project & Testing

### File & Folder Penting

```text
main.py                                   Entry point aplikasi
.env                                      Kredensial rahasia (tidak ikut Git)
cv/                                       Taruh CV asli di sini (tidak pernah diubah)
config/
  settings.yaml                           Konfigurasi filter, mode, batas kirim, dan sumber
  email_templates.yaml                    Template struktur email (EN & ID)
  search_profiles.yaml                    Board ATS, daftar portal, dan kata kunci pencarian
  skills_taxonomy.yaml                    Daftar skill untuk pencocokan CV & lowongan
src/
  cli/                                    Interactive Hub (hub.py), CLI search & apply
  cv/                                     Parser PDF/DOCX/TXT, analyzer, struktur dokumen CV
  discovery/ & sources/                   Konektor ATS, feed remote, HN, Brave, career pages
  extraction/ & intelligence/             Ekstraksi konten/kontak, verifikasi, dedup, matching
  applications/                           Email generator, Custom CV PDF, fact-check, Gmail/SMTP, tracker
  reporting/ & storage/                   Generator DOCX/XLSX dan database SQLite
data/                                     Database SQLite, generated CVs, outbox, cache, log
reports/                                  Laporan hasil pencarian (DOCX/XLSX/JSON)
tests/                                    Unit & integration tests
```

### Menjalankan Test

```powershell
pip install -r requirements-dev.txt
python -m pytest                                                      # offline suite: 243 passed, 1 skipped
$env:RUN_LIVE_TESTS="1"; python -m pytest tests/integration/test_live_optin.py   # test jaringan opsional
```

Detail arsitektur teknis terdahulu juga terdokumentasi di [FINAL_DEVELOPMENT_REPORT.md](FINAL_DEVELOPMENT_REPORT.md).
