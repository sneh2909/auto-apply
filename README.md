# auto-apply-pipeline

Semi-automated job-application pipeline for ML/AI roles in India. See [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) for the full design.

## Status

Scraping: Gemini/Groq browser agent runs across LinkedIn, Naukri, Foundit, and Instahyre in parallel.  
Email flow: 2-click approve (Generate Draft → preview/edit → Send).  
Channels B/C (email) working; A (ATS autofill) and D (browser agent) are partial stubs.

## Prerequisites

- **Python 3.12+**
- **[uv](https://github.com/astral-sh/uv)** (recommended) or pip
- **MongoDB** — local (`mongodb://localhost:27017`) or [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
- **Chromium** for Playwright (`playwright install chromium`)
- A **dedicated Gmail account** for sending (not your primary inbox)
- API keys for at least **scoring** (Gemini) and **email composition** (Groq) — see [LLM providers](#llm-providers)

## Setup

### 1. Clone and install

**Windows (PowerShell)**

```powershell
cd auto-apply-pipeline
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
playwright install chromium
```

**Linux / macOS / WSL**

```bash
cd auto-apply-pipeline
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
# WSL2 only — system libs for headless Chromium:
sudo playwright install-deps chromium
playwright install chromium
```

### 2. Environment variables

```bash
cp .env.example .env
```

Edit `.env`. Minimum to get started:

| Variable | Purpose |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `GMAIL_USER_EMAIL` | Dedicated sender Gmail address |
| `GMAIL_SENDER_NAME` | Display name on outbound mail |
| `SCORER_API_KEY` | Google Gemini API key (scoring) |
| `COMPOSER_API_KEY` | Groq API key (email drafts) |
| `AGENT_PROVIDER` | Set `groq` to avoid Gemini quota on scraping |
| `HEADLESS_BROWSER` | `1` = background scrapers, `0` = visible browser |

### 3. Config YAML files

Edit the files under `config/` directly (no separate `.example` copies):

| File | Purpose |
|---|---|
| `config/profile.yaml` | Identity, skills, target roles (required) |
| `config/blocklist.yaml` | Companies/domains to skip |
| `config/qa_bank.yaml` | ATS form auto-answers |
| `config/companies.yaml` | Optional Greenhouse/Lever slugs (`INGEST_ATS_SOURCES=1`) |
| `config/portals.yaml` | Portal logins — created via `/credentials` UI (gitignored) |

Also place your resume at `config/resume.pdf` (gitignored).

### 4. Credentials (one-time)

These files are **gitignored** — see [What not to commit](#what-not-to-commit).

| Step | Command / file | Notes |
|---|---|---|
| Gmail OAuth client | Download Desktop OAuth JSON → `config/gmail_credentials.json` | [Google Cloud Console](https://console.cloud.google.com/) → Gmail API → OAuth 2.0 Desktop |
| Gmail token | `python scripts/setup_gmail_oauth.py` | Opens browser; saves `data/gmail_token.json` |
| LinkedIn session | `python scripts/setup_linkedin_login.py` | Sign in in the browser window; cookies persist in `data/linkedin-profile/` |
| Other portals | `python scripts/setup_portal_login.py naukri` | Same for `foundit`, `instahyre`, `linkedin` |
| Portal email/password | Web UI → http://127.0.0.1:8765/credentials | Or edit `config/portals.yaml` |
| Vertex (optional) | `config/service_account.json` | Set `SCORER_SERVICE_ACCOUNT_JSON` / `AGENT_SERVICE_ACCOUNT_JSON` in `.env` |

Verify LinkedIn session:

```bash
python scripts/debug_linkedin_session.py
```

## Run

### Web console (recommended)

```bash
autoapply review
```

Open http://127.0.0.1:8765

- **Run full pipeline** — ingest → score → route  
- **Ingest jobs** / **Score jobs** / **Route applications** — individual steps  
- **Credentials** — Gmail OAuth, portal logins  
- **Blocklist** — http://127.0.0.1:8765/blocklist  

Keep the terminal open while using the UI. Stop with `Ctrl+C`.

If `MONGO_URI` points to Atlas, the machine needs network access at startup (index creation).

### CLI

```bash
autoapply ingest          # fetch jobs from enabled sources
autoapply score           # score unscored jobs (--limit 100)
autoapply route           # create application rows (--limit 50)
```

### Background scheduler (optional)

```bash
# Linux/macOS
nohup autoapply scheduler > data/logs/scheduler.log 2>&1 &
```

Runs ingest every 30 minutes; score/route every 60 minutes.

### Disable a portal

In `.env`, trim the list, e.g.:

```env
SCRAPER_PORTALS=["linkedin","naukri"]
```

LinkedIn requires `setup_linkedin_login.py` first. Naukri/Foundit/Instahyre can use manual or stored credentials on `/credentials`.

## LLM providers

| Role | Provider | Model | Key env var |
|---|---|---|---|
| Job scoring | Google Gemini | `gemini-2.0-flash` | `SCORER_API_KEY` |
| Email composition | Groq | `llama-3.3-70b-versatile` | `COMPOSER_API_KEY` |
| Browser agent | Groq / Gemini / Sarvam | see `.env.example` | `AGENT_API_KEY`, `COMPOSER_API_KEY`, or `SARVAM_API_KEY` |

No LiteLLM — native `google-genai`, `groq`, and Sarvam HTTPS APIs.

For scraping, prefer `AGENT_PROVIDER=groq` and `AGENT_MODEL=groq/llama-3.3-70b-versatile` to avoid Gemini free-tier quota during ingest.

Service-account auth: set `SCORER_SERVICE_ACCOUNT_JSON=./config/service_account.json` (same for `AGENT_*`); uses Vertex with `SCORER_GOOGLE_CLOUD_LOCATION` / `AGENT_GOOGLE_CLOUD_LOCATION` (default `us-central1`).

## Sources

| Source | Status |
|---|---|
| LinkedIn / Naukri / Instahyre Gmail alerts | ✅ |
| **Jobs2Web talent-community emails** (Deloitte, Mahindra, etc.) | ✅ |
| Portal scrapers (LinkedIn, Naukri, Foundit, Instahyre) | ✅ — profile roles + cities, past-week filter |
| LinkedIn hiring posts (DOM + email extraction) | ✅ |
| Google Jobs open-web discovery | ✅ (`WEB_DISCOVERY_ENABLED=1`) |
| LLM email + job extraction on scraped pages | ✅ (`SCRAPER_LLM_ENRICH=1`, default on) |
| Greenhouse / Lever public APIs | Optional (`INGEST_ATS_SOURCES=1` + `companies.yaml` slugs) |
| Hirect / Wellfound / Ashby | 🟡 stubs |

**Ingest filtering:** Every job is checked against `profile.yaml` (target roles + cities / remote) and `blocklist.yaml` before it is saved. You do not need a curated `companies.yaml` list for day-to-day use — join talent communities on company career sites and let Gmail alerts flow in.

**Gmail setup for corporate career sites:** On sites like `southasiacareers.deloitte.com` or `jobs.mahindracareers.com`, create a job agent (role + location). Alerts from `*@*.jobs2web.com` land in Gmail → label them `JobAlerts/Unprocessed` → ingest picks them up.

HR email resolution: extract from source → regex on JD → careers page → `careers@<domain>` fallback. No paid enrichment APIs.

## Storage

**MongoDB** (`MONGO_URI`):

| Collection | Contents |
|---|---|
| `jobs` | Discovered roles, scores, HR email |
| `applications` | Review queue (`queued` → `sent`) |
| `agent_runs` | Channel D sessions |
| `source_runs` | Ingest audit log |

**Filesystem:**

| Path | Contents |
|---|---|
| `config/profile.yaml` | Your profile for LLM context (edit in repo) |
| `config/blocklist.yaml` | Blocked companies/domains |
| `config/qa_bank.yaml` | ATS answers |
| `config/companies.yaml` | ATS scan targets |
| `config/resume.pdf` | Resume attachment (gitignored) |
| `config/portals.yaml` | Portal login credentials (gitignored) |
| `data/linkedin-profile/` | LinkedIn session cookies |
| `data/*-profile/` | Other portal browser profiles |
| `data/gmail_token.json` | Gmail OAuth refresh token |
| `data/logs/` | Scheduler logs |

## What not to commit

Never add these to git (they are listed in `.gitignore`):

| Path | Why |
|---|---|
| `.env` | API keys, `MONGO_URI`, Gmail address |
| `config/gmail_credentials.json` | Google OAuth client secret |
| `config/service_account.json` | GCP private key |
| `config/portals.yaml` | Portal passwords (after you fill them in) |
| `config/resume.pdf` | Your resume PDF |
| `data/` | OAuth tokens, browser sessions, logs |

Safe to commit: `.env.example`, `config/profile.yaml`, `config/blocklist.yaml`, `config/qa_bank.yaml`, `config/companies.yaml` (use placeholders — avoid committing real phone/email if the repo is public).

If you ever committed a secret, rotate the key in the provider console, then remove it from git history (`git filter-repo` or BFG) — changing `.gitignore` alone does not revoke leaked credentials.

## Layout

```
src/autoapply/
  config.py            # .env + YAML loaders
  db/                  # MongoDB models
  sources/             # job discovery
  score/               # LLM scoring + email composer
  channel/             # routing, email send, ATS autofill
  review/              # FastAPI review UI
  scheduler.py         # periodic ingest/score/route
  cli.py               # autoapply <subcommand>
scripts/
  setup_gmail_oauth.py
  setup_linkedin_login.py
  setup_portal_login.py
  debug_linkedin_session.py
config/
  profile.yaml         # edit with your details
  blocklist.yaml
  qa_bank.yaml
  companies.yaml
```

## Safety

- Daily send cap in code (default `DAILY_SEND_CAP=10`).
- All sends require manual approval in the review queue.
- Use a dedicated Gmail account, not your primary.
- LinkedIn scraper only reads your feed — it does not post, connect, or message.

## Troubleshooting

**Port 8765 already in use**

```powershell
# Windows
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

```bash
# Linux/macOS
kill -9 $(lsof -t -i :8765) 2>/dev/null || true
```

**LinkedIn session expired** — re-run `python scripts/setup_linkedin_login.py` (needs a visible browser; set `HEADLESS_BROWSER=0` if needed).

**Gmail token invalid** — re-run `python scripts/setup_gmail_oauth.py`.
