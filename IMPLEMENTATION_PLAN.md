# Auto-Apply Pipeline — Implementation Plan

> Status: **Draft v1** — design locked, not yet implemented. Iterate this doc as scope shifts.

## 1. North Star

A semi-automated job-application system that turns ~200 daily inbound job signals into **10 high-quality applications/day** with one-click human approval. India-focused (Bangalore/Hyderabad/Pune/remote-India) for ML/AI roles.

For each surviving role the system picks the **best available application channel**:

| Channel | When used | Mechanism |
|---|---|---|
| **A — ATS auto-fill** | URL matches Greenhouse / Lever / Ashby / Naukri easy-apply | Playwright fills form, uploads resume, submits |
| **B — Email reply** | Alert email contains a reply-capable HR/recruiter address | Tailored email via Gmail API |
| **C — Cold HR email** | We resolve a recruiter inbox via cascade | Tailored email via Gmail API |
| **D — Agentic DOM** | You type a natural-language goal in the dashboard, or a job sits on a long-tail site without an A/B/C path | LLM-driven Playwright agent (`browser-use`) navigates, searches, applies |
| **Skip** | None of the above resolves *or* fit-score below threshold | Logged, not applied |

## 2. Operating constraints

- **10 applications/day cap**, every day (incl. weekends — your choice).
- **Human review queue** for first 2 weeks across all channels; later we can flip high-confidence channel-A jobs to auto-submit.
- **Free tools only.** No Hunter.io / Apollo.io / SerpAPI / RocketReach / paid scrapers. HR-email finding relies on extraction from source payloads (LinkedIn post text, alert bodies) + careers-page scrape + `careers@<domain>` fallback.
- **Single LLM-agnostic abstraction** so you can switch between Gemini / Groq / Claude / Ollama via `.env`.
- **Indian market focus** — top sources: **LinkedIn hiring posts** (highest HR-email yield), **Naukri / Instahyre alert emails**, **Greenhouse / Lever** ATS public APIs.
- **Dedicated Gmail account** (not your primary) for sending — protects your reputation and lets us be more aggressive with limits.
- **DPDP Act 2023** — include opt-out sentence in cold emails, never email the same person twice, no impersonation.

## 3. Architecture

Pure Python. One repo, one Docker compose file if you want to deploy. No n8n.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Scheduler                                  │
│                          (APScheduler, in-process)                      │
└─┬───────────────┬─────────────────┬──────────────────┬──────────────────┘
  │ every 30m     │ every 1h        │ every 1h         │ every 10m
  ▼               ▼                 ▼                  ▼
[Ingestion]   [Scoring]         [Channel router]   [Reply watcher]
  │ Gmail        │ LLM scores       │ A: Playwright     │ Gmail labels
  │ + ATS APIs   │ JD vs profile    │ B/C: drafter      │ → classify
  ▼               ▼                 ▼                  ▼
              ┌───────────────────────────────┐
              │       SQLite (SQLModel)       │
              │  jobs · applications · replies│
              └───────────┬───────────────────┘
                          │
                          ▼
              [Review UI — FastAPI + HTMX]
                          │
                          ▼
              [Sender — Gmail API / Playwright submit]
```

## 4. Repo layout

```
auto-apply-pipeline/
├── pyproject.toml           # uv / poetry, Python 3.12
├── .env.example
├── README.md
├── IMPLEMENTATION_PLAN.md   # this file
├── config/
│   ├── profile.yaml         # your "about me" — fed to every LLM call
│   ├── resume.pdf           # canonical resume attached to outgoing mail
│   ├── companies.yaml       # seed list of target companies (priority ATS scan)
│   ├── blocklist.yaml       # companies / domains we never apply to
│   └── qa_bank.yaml         # answers to common ATS questions (notice period, ctc, …)
├── src/autoapply/
│   ├── __init__.py
│   ├── config.py            # pydantic-settings: .env + yaml load
│   ├── db/
│   │   ├── models.py        # SQLModel: Job, Application, Reply, SourceRun
│   │   └── migrations/      # alembic
│   ├── sources/             # *every* inbound flow normalises to JobRecord
│   │   ├── base.py
│   │   ├── gmail_alerts.py  # OAuth, label-driven fetch
│   │   ├── parsers/
│   │   │   ├── linkedin.py
│   │   │   ├── naukri.py
│   │   │   ├── instahyre.py
│   │   │   ├── hirect.py
│   │   │   ├── foundit.py
│   │   │   └── wellfound.py
│   │   └── ats/
│   │       ├── greenhouse.py    # public boards API
│   │       ├── lever.py
│   │       └── ashby.py
│   ├── score/
│   │   ├── llm.py            # LiteLLM-style provider abstraction
│   │   ├── prompts/
│   │   └── scorer.py         # JD + profile → {score, reasons, deal_breakers}
│   ├── channel/
│   │   ├── router.py         # picks A/B/C for each job
│   │   ├── autofill/
│   │   │   ├── base.py       # Playwright session manager
│   │   │   ├── greenhouse.py # known field map + qa_bank lookup
│   │   │   ├── lever.py
│   │   │   └── naukri.py     # easy-apply
│   │   └── email/
│   │       ├── composer.py   # JD + profile + recruiter → 4-sentence email
│   │       ├── hr_resolver.py# cascade: existing hr_email → JD regex → careers-page scrape → careers@<domain>
│   │       └── sender.py     # Gmail API send with resume attached
│   ├── review/
│   │   ├── app.py            # FastAPI
│   │   ├── templates/        # Jinja + HTMX
│   │   └── static/
│   ├── tracker/
│   │   ├── reply_watcher.py  # Gmail label "applied-out" replies → classify
│   │   └── stats.py
│   └── scheduler.py          # `python -m autoapply.scheduler`
├── tests/
│   ├── fixtures/             # sample alert emails per source
│   ├── test_parsers.py
│   ├── test_scorer.py
│   └── test_router.py
└── scripts/
    ├── setup_gmail_oauth.py
    ├── seed_companies.py
    └── one_off_score.py      # debug: score a JD interactively
```

## 5. Data model

**Storage**: MongoDB via **pymongo's native async client** (`AsyncMongoClient`, available in pymongo 4.9+). No Beanie, no Motor — pymongo's own async API is sufficient and keeps the dep tree small. Pydantic models are plain data classes with a thin `MongoDoc` mixin providing `insert / save / find_one / find_many`. Local Mongo for dev (`mongodb://localhost:27017`); Atlas free tier for production.

Collections — see `src/autoapply/db/models.py` for the live schema:

- **`jobs`** — normalized postings from any source. Unique on `dedup_hash`; compound index on `(source, source_id)`.
- **`applications`** — one attempt per (job × channel). Tracks draft / status / sent_at / error.
- **`replies`** — inbound mail classified by LLM (`interest | reject | auto_ack | other`).
- **`agent_runs`** — natural-language sessions of Channel D, including transcript path and discovered/applied job lists.
- **`source_runs`** — audit log per ingestion run; lets us detect parser drift (sudden drop in emit-rate vs emails-seen).

Channels are an enum: `ats | email | coldmail | agent` (A / B / C / D in plan-speak).

## 6. Phases & estimates

Calibrated for a working ML engineer doing this evenings/weekends.

### Phase 0.5 — Dashboard credential upload (NEW, deferred)
Web UI to upload `resume.pdf`, paste API keys, drop `gmail_credentials.json`, run OAuth, edit `profile.yaml`. Stops `.env` editing entirely. Secrets stored in `data/secrets.json` (gitignored, 0600); FastAPI bound to 127.0.0.1 only.
**Status**: deferred per user instruction; currently after Phase 1a (LinkedIn scraper) in priority.

### Phase 1a — LinkedIn-posts scraper (PRIORITY — implemented)
- Playwright with persistent profile (`data/linkedin-profile/`) reusing your LinkedIn cookies.
- `setup_linkedin_login.py` bootstrap script (one-time, headful — handle 2FA yourself).
- Per query in `settings.linkedin_search_queries`, scrape content-search results, scroll N pages, extract post cards, regex emails, build `JobRecord(hr_email=...)`.
- Highest HR-email-yield source for the India ML market: recruiters publish emails intentionally in feed posts.

### Phase 0 — Bootstrap (1 evening)
- New Gmail / Workspace alias for sending; enable Gmail API; download OAuth credentials.
- Write `profile.yaml` (template provided in repo).
- Drop `resume.pdf`.
- Seed `companies.yaml` with ~50 India-focused ML targets.
- Sign up for free-tier keys: **Gemini** (scoring), **Groq** (composing). No HR-email service — we extract from source payloads + careers-page scrape.
- `uv venv && uv pip install -e .` — repo runs.

### Phase 1 — Ingestion + storage (1 weekend)
- Gmail API client with offline OAuth refresh.
- Label-driven fetch: anything in Gmail label `JobAlerts/Unprocessed` → ingest → move to `JobAlerts/Processed`.
- Parsers for Naukri, Instahyre, Hirect, Foundit, Wellfound, LinkedIn alert formats.
- SQLite + Alembic migrations.
- Dedup logic.
- **Tests**: 5 captured emails per source live in `tests/fixtures/`; parsers verified offline.
- Exit criteria: 100 alert emails → 100 deduped Job rows in DB.

### Phase 2 — Scoring (1 evening)
- LLM abstraction `score.llm.LLM(provider, model)` supporting `gemini`, `groq`, `anthropic`, `ollama`.
- Scorer prompt returns strict JSON via response-format.
- Override list (`profile.yaml: always_apply: [acme, beta]`) → score = 10.
- Threshold (default 6.5) configurable in `.env`.
- Exit criteria: 100 jobs scored, top-10 vs bottom-10 spot-check passes your eyeball test.

### Phase 3 — Channel A: ATS auto-fill (2 weekends)
- Playwright session manager (persistent user-data-dir under `~/.autoapply/browser/` so cookies survive).
- Field-map for **Greenhouse** (handles ~80% of YC + global tech that India-remote-friendly companies use).
- Field-map for **Lever**.
- Field-map for **Naukri easy-apply** (test against your existing Naukri login).
- `qa_bank.yaml` lookup for custom questions (notice period, current ctc, expected ctc, why-this-company-template).
- Headful + slow-motion by default; `HEADLESS=1` env var to flip later.
- **Exit criteria**: 5 successful submissions per ATS without manual intervention.

### Phase 4 — Channels B & C: email drafter (1 weekend)
- `hr_resolver` cascade — **free tools only**:
  1. `hr_email` already present on the Job (extracted by the source: LinkedIn post regex, Instahyre alert body, etc.).
  2. Regex over `jd_text` (catches mid-body "email me at recruiter@company.com" in LinkedIn posts and Naukri JD blurbs).
  3. Scrape the company careers / contact / about page (`httpx` + `selectolax`) and pull any visible non-platform email.
  4. Fallback `careers@<domain>`.
- Composer prompt → 4-sentence email referencing 2 JD specifics + 1 profile bullet. JSON output: `{subject, body, confidence}`.
- Resume attached from `config/resume.pdf`.
- Saved as Gmail Draft in dedicated account; not sent until approved.

### Phase 4.5 — Channel D: natural-language agentic mode (1 weekend)
- Library: [`browser-use`](https://github.com/browser-use/browser-use) — Playwright + LLM agent loop with DOM compression.
- Dashboard text box: "Tell the agent what to do" → e.g. *"apply to all senior data science roles in Bangalore on Wellfound"*.
- Agent runs in **two passes**:
  1. **Discovery**: agent searches / navigates / collects candidate jobs → writes them as `Job` rows (same `dedup_hash` and scoring as Tier-1 sources).
  2. **Apply**: for each surviving job (fit-score over threshold, within daily cap, not already applied), routes through channel A if ATS-shaped, otherwise queues for channel B/C.
- Tracked in new `AgentRun` table: `goal`, `started_at`, `steps_taken`, `jobs_discovered`, `jobs_applied`, `status`, `last_error`, `transcript_path`.
- **Guardrails**:
  - Max 80 steps per run, max 5 min wallclock, max 20 candidates before forcing review.
  - Hard pause + dashboard screenshot on login screen, captcha, or unknown modal.
  - Submit clicks gated by review queue during the first 2 weeks.
  - Goal disambiguation: agent first emits structured `{role_query, location, site, count_limit}` and asks for confirmation.
- **Cost**: ~50 steps × 2K tokens. Free on Gemini Flash; ~$0.30 on Claude Sonnet.
- **Why not just use this for everything?** Slower (~30 s/job vs 5 s deterministic), pricier per step, less reliable on repeat runs. Use it for the long tail and natural-language ad-hoc goals; use deterministic channel A for the 80% case.

### Phase 5 — Review UI (1 weekend)
- FastAPI + Jinja + HTMX (no React).
- Single page `/queue` shows pending applications sorted by `fit_score desc`.
- Per row: company, role, score, channel, recipient or ATS URL, 3-line draft preview, buttons `[Approve] [Edit] [Skip]`.
- `/edit/{id}` inline editor for subject/body.
- Daily-cap counter at top of page: `Approved today: 4 / 10`. Approve button disabled at cap.
- Approve action:
  - Channel A → enqueue Playwright submit task.
  - Channels B/C → Gmail API send.
- **Exit criteria**: end-to-end approve → application visible in your "Sent" folder (or ATS confirmation page).

### Phase 6 — Reply tracking (1 evening)
- Background watcher polls Gmail every 10 min for messages where `in:inbox` has `In-Reply-To` matching a sent application.
- LLM classifier: snippet → `interest|reject|auto_ack|other`.
- Stats endpoint: applications sent, replies received, interest-rate, broken down by source/channel.

### Phase 7 — Production polish (1 weekend)
- `docker-compose.yaml`: one container (app), one volume (sqlite + browser-data + logs).
- systemd unit alternative for VPS deployment without Docker.
- Structured logging via `structlog`.
- Optional Telegram bot: `/queue` shows top 3 pending, `/approve <id>` shortcut.
- Backup script: nightly `sqlite3 .dump` → committed to a private git repo.

**Total**: ~6 weekends + 4 evenings = ~2 months part-time.

## 7. Profile & config files

### `config/profile.yaml`

```yaml
identity:
  full_name: ""
  email_primary: ""           # the dedicated sender Gmail
  phone: ""
  linkedin: ""
  github: ""
  portfolio: ""
  location: "Bangalore, IN"
  willing_to_relocate: ["Hyderabad", "Pune", "Remote"]

career:
  years_experience: 0
  current_title: ""
  current_company: ""
  notice_period_days: 30
  current_ctc_inr: 0          # used for ATS question auto-answer
  expected_ctc_inr: 0
  work_authorisation: "Indian citizen"

target:
  roles: ["ML Engineer", "MLOps Engineer", "Applied Scientist", "Deep Learning Engineer"]
  seniority: ["Senior", "Staff", "Lead", "Mid"]
  industries_priority: ["AI startups", "Fintech", "Healthtech"]
  industries_avoid: ["Adtech"]
  remote_ok: true
  on_site_cities_ok: ["Bangalore", "Hyderabad", "Pune"]
  salary_min_inr: 0
  deal_breakers:
    - "Mandatory on-site outside listed cities"
    - "No remote on Fridays"
    - "Pure data-engineering role"

skills:
  primary: ["PyTorch", "Python", "LLMs", "Transformers", "MLOps"]
  secondary: ["TensorFlow", "JAX", "Kubernetes", "AWS Sagemaker"]
  domains: ["NLP", "Computer Vision", "Recommender Systems"]

highlight_bullets:                # the LLM picks the most JD-relevant ones per email
  - "Built X serving Y QPS at company Z, cut p99 latency 40%"
  - "..."

always_apply: []                  # company slugs that bypass scoring
qa_overrides:                     # things the LLM should always say
  why_this_role_pattern: "..."

llm:
  scorer_provider: "gemini"
  scorer_model: "gemini-2.0-flash"
  composer_provider: "groq"
  composer_model: "llama-3.3-70b-versatile"
  fit_threshold: 6.5
  daily_cap: 10
```

### `config/qa_bank.yaml`

```yaml
# Auto-answers for ATS custom questions; LLM falls back to profile.yaml.
"notice period":             "30 days"
"current ctc":               "from profile.career.current_ctc_inr"
"expected ctc":              "from profile.career.expected_ctc_inr"
"are you authorized to work in India": "Yes"
"how did you hear about us": "Company website"
"willing to relocate":       "from profile.identity.willing_to_relocate"
```

## 8. LLM choice & cost

Defaults set for **zero cost** at 10 apps/day:

| Step | Provider | Model | Cost/day |
|---|---|---|---|
| Scoring (every job ingested) | **Gemini Free** | `gemini-2.0-flash` | Free tier covers ~200 calls/day |
| Composing email | **Groq Free** | `llama-3.3-70b-versatile` | Free tier covers ~25 calls/day |
| Reply classification | **Gemini Free** | `gemini-2.0-flash` | Negligible |
| Custom-question answering during ATS fill | **Groq Free** | same | Negligible |

If free tier exhausts, swap composer to **Claude Haiku** (~$0.01/email = $0.10/day). LLM provider is a one-line `.env` change.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Naukri/LinkedIn change alert email HTML | Each parser tested against captured fixtures; weekly cron runs parsers over yesterday's emails and alerts on parse-rate drop |
| ATS form structure changes (selector breakage) | Multiple selector fallbacks per field; smoke test runs daily against a known sample job |
| Gmail flags account as bulk sender | 10/day hard cap; warm-up week of 3/day; dedicated account; vary subject lines; never reuse a body verbatim |
| HR email not resolvable for a job | Skip cold-email channel for that job and either auto-fill the apply URL (channel A) or wait for the agent (channel D) — never guess emails with paid lookups |
| LLM produces hallucinated/embarrassing claims about you | Review queue catches before send; composer prompt explicitly says "use only facts from profile.yaml" |
| DPDP Act 2023 (unsolicited commercial email) | Include opt-out line; never email same person twice; honour any "remove me" reply automatically |
| ATS bot detection | Headful Playwright with persistent user-data-dir, random typing delays, human-like mouse movement (`playwright_stealth`) |
| Captcha on Naukri/LinkedIn | Bot pauses, sends Telegram notification with screenshot, you solve manually within 10 min |
| Duplicate applications | DB-level UNIQUE on `(company_normalized, role_normalized)` + 30-day cooldown |

## 10. What's explicitly out of scope (for v1)

- Scraping LinkedIn job listings directly (TOS, fragile) — we rely on the alert emails LinkedIn sends you.
- Reaching out via LinkedIn InMail / messages.
- Resume tailoring per JD (we send the same PDF). Could add later as Phase 8.
- Auto-scheduling phone screens / interview prep.
- Multi-user / shared deployment — this is single-user for you.
- Mobile UI. Review queue is desktop-only.

## 11. What I need from you to start Phase 0

1. **Resume PDF** → drop into `config/resume.pdf`.
2. **Decide on sender email**: dedicated `<yourname>.apply@gmail.com` (free, easiest) or Workspace alias on your own domain (best deliverability, ~$6/mo).
3. **API key signups** (free tier only): Gemini AI Studio, Groq Cloud. (No HR-email service.)
4. **Filled `profile.yaml`** — I'll generate the template; you fill in.
5. **Confirm** this repo lives at `/mnt/d/Work/Projects/auto-apply-pipeline/` (already created).

## 12. Decisions still open (deferrable until the relevant phase)

- Persistent browser profile vs fresh-session for Playwright (affects how Naukri / LinkedIn treat your bot).
- Telegram bot for review vs web-only.
- Whether to add a "cover letter PDF tailored per JD" upgrade in Phase 8.
- Whether to capture a small dataset of (JD, my-resume, outcome) so we can fine-tune a small open scorer later (an actual ML side-project hiding inside this tool).

---

**Next step when you say go:** I scaffold the repo (pyproject, package skeleton, `profile.yaml` template, OAuth bootstrap script) and we tackle Phase 1 first.
