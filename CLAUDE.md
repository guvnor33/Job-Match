# JobScrape

A personal job-listing aggregator that pulls listings from multiple sources, stores them in a local SQL database, and presents them in a web UI with application-tracking and AI-powered resume matching.

---

## Project Goals

- Aggregate job listings from three sources:
  1. **Gmail** — parse job alert / recruiter emails via the Gmail API
  2. **LinkedIn** — search and scrape listings (search terms configurable)
  3. **Company career pages** — a user-maintained list of direct URLs to company job boards
- Store all listings in a local SQLite database (single file, no server required)
- Serve a web UI to browse listings and mark each as applied / not applied
- Allow adding new company career-page URLs directly from the web UI (paste-and-save)
- Evaluate each listing against the user's resume via the Claude API and display a match score (red → yellow → green bar)

---

## Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | single language across all components |
| Database | SQLite (via `sqlite3` std lib) | local file `jobscrape.db` |
| Web framework | Flask | lightweight, easy to run locally |
| Gmail integration | Google Gmail API (`google-api-python-client`) | OAuth2, reads inbox |
| LinkedIn scraping | `playwright` (headless browser) | LinkedIn blocks simple HTTP; persistent context for login |
| Career-page scraping | `httpx` + `beautifulsoup4` first | fall back to Playwright for JS-rendered pages |
| Scheduling | `apscheduler` | runs scrapers on a cron-like schedule |
| Frontend | Vanilla HTML/CSS + HTMX | no build step, instant interactivity |
| AI matching | Claude API (`anthropic` SDK) | compares job description against resume; returns 0–100 score |

---

## Repository Layout

```
JobScrape/
├── CLAUDE.md
├── jobscrape.db            # SQLite database (gitignored)
├── requirements.txt
├── config.yaml             # user config: LinkedIn search terms, career-page URLs, schedule
├── .env                    # secrets: OAuth tokens, Anthropic API key (gitignored)
├── resume.txt              # plain-text resume used for AI matching (gitignored)
│
├── scrapers/
│   ├── __init__.py
│   ├── gmail_scraper.py    # Gmail API ingestion
│   ├── linkedin_scraper.py # Playwright-based LinkedIn search
│   └── career_page_scraper.py  # httpx+BS4 scraper, Playwright fallback
│
├── matching/
│   ├── __init__.py
│   └── resume_matcher.py   # calls Claude API; returns match score + short rationale
│
├── db/
│   ├── __init__.py
│   ├── schema.sql          # DDL for all tables
│   └── models.py           # thin wrapper functions (no ORM)
│
├── web/
│   ├── app.py              # Flask app entry point
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html      # listing table: applied checkbox + match score bar
│   │   ├── detail.html     # single listing detail view + AI rationale
│   │   └── sources.html    # manage career-page URLs (paste new URL → save)
│   └── static/
│       └── style.css
│
└── run.py                  # top-level runner: starts scheduler + Flask
```

---

## Database Schema (core tables)

```sql
-- jobs: one row per unique listing
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- 'gmail' | 'linkedin' | 'career_page'
    company       TEXT,
    title         TEXT,
    location      TEXT,
    url           TEXT UNIQUE,
    description   TEXT,
    posted_at     TEXT,                   -- ISO-8601 date string from source
    fetched_at    TEXT NOT NULL,          -- ISO-8601 datetime we ingested it
    applied       INTEGER NOT NULL DEFAULT 0,  -- 0=no, 1=yes
    notes         TEXT,
    match_score   INTEGER,               -- 0–100, NULL until evaluated
    match_reason  TEXT                   -- short Claude-generated rationale
);

-- career_page_sources: the user-maintained URL list (also editable via web UI)
CREATE TABLE career_page_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    url         TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1
);
```

---

## Configuration

The full annotated `config.yaml` reference lives in **`SETUP.md` → Configuration**
(`schedule`, `linkedin`, `gmail`, `career_pages`, `matching`). Note: LinkedIn/Gmail
search terms and the blacklist are now primarily managed in `search_terms.toml` (see
Design Decisions below); `config.yaml` holds the remaining settings.

---

## Key Implementation Notes

### Deduplication
Use `url` as the unique key. On conflict (same URL seen again), update `fetched_at` but preserve `applied`, `notes`, `match_score`, and `match_reason`.

### LinkedIn (Playwright)
- Use a **persistent browser context** stored on disk (e.g. `linkedin_session/`)
- First run: launch headed, log in manually, then save context
- Subsequent runs: load saved context — no login prompt
- Do not use headless mode if getting blocked

### Gmail OAuth Setup
See **`SETUP.md` → Gmail OAuth Setup** for the full Google Cloud Console walkthrough.
Scope needed: `https://www.googleapis.com/auth/gmail.readonly`

### Career Page Scraping
- **Default**: `httpx` GET → BeautifulSoup parse for `<a>` tags containing job-like text
- **Fallback**: if the page body is mostly empty after BS4 parse (JS-rendered), re-fetch with Playwright
- Selectors can be specified per-URL in `config.yaml` under a `selectors` sub-key:
  ```yaml
  career_pages:
    - company: "Acme Corp"
      url: "https://acme.com/careers"
      selectors:
        job_link: "a.job-listing"    # optional CSS selector override
  ```

### App settings live in `settings.toml` (NOT JSON, NOT config.yaml)
All mutable app settings are persisted by `app_settings.py` to **`settings.toml`**
(read with `tomllib`, written with `tomlkit` — same convention as `search_terms.toml`).
The `[scoring]` table holds: `scoring_enabled`, `auto_score_on_ingest`, `scoring_backend`,
`claude_model`, `rationale_detail`, `local_base_url`, `local_model`. On first load it
auto-migrates from the legacy `app_settings.json` (now deleted) and seeds `claude_model`
/`auto_score_on_ingest` from `config.yaml`'s `matching` block, so the scoring path no
longer reads `config.yaml` at all. **Anti-wipe guard:** `set_scoring_backend` IGNORES
empty `base_url`/`model` so a stray blank submit can't erase a working local config (an
earlier JSON bug had wiped these to `""`, which made local scoring silently no-op).

### Global scoring kill-switch (API cost control)
`app_settings.py` persists runtime scoring settings to `settings.toml` (separate
from `config.yaml` because these are UI toggles, not static config).
`score_job_if_enabled()` checks `scoring_enabled` FIRST as a master gate — returns
`"scoring OFF (global)"` and skips scoring entirely when off. Toggled from the Run
Scrapers page (`/settings/toggle-scoring`, HTMX checkbox, partial
`_scoring_settings.html`). Lets the user scrape/test without spending API money.
Note: LinkedIn's own free match assessment (`li_match`, below) is still captured when
scoring is off — it costs nothing.

### Scoring backend: Claude API or local LLM
The scorer supports two backends, chosen in the UI (Run Scrapers → Scoring engine,
`/settings/scoring-backend`):
- `claude` (default): paid Anthropic API (`_call_claude`, model from `config.yaml`).
- `local`: a locally-running, OpenAI-compatible server (Ollama, LM Studio, llama.cpp,
  vLLM) — FREE, no API cost. `_call_local` POSTs to `{base_url}/chat/completions` via
  `httpx` (base_url + model stored in `app_settings.json`, e.g.
  `http://localhost:11434/v1` + `llama3.1`). Sends `response_format=json_object` and
  retries once without it for servers that reject it.
**Feedback detail** is a UI setting (`rationale_detail`: brief/standard/detailed, in the
same scoring form): `_build_prompt` swaps the `reason` instruction (`_RATIONALE_INSTRUCTIONS`)
and `score_job` scales `max_tokens` (`_RATIONALE_MAX_TOKENS`). "detailed" (default) asks
the model to name specific missing skills/qualifications. The listings inline preview
truncates at 220 chars; the detail page shows the full rationale.
`resume_matcher.score_job()` builds one shared prompt (`_build_prompt`), dispatches to
the chosen backend, then parses with `_parse_result` — which tolerates markdown fences
and surrounding prose (local models are chattier) by extracting the first `{...}`.
`test_local_connection(base_url, model)` powers the UI "Test connection" button
(`/settings/test-local`) so the user can verify their local server before relying on it.
`list_local_models(base_url)` powers the "Detect model" button (`/settings/detect-models`):
it GETs the server's `/models` endpoint and populates a `<datalist>` of model ids for the
Model field. It only auto-fills when the field is BLANK — it never overwrites an existing
name, because some servers report an odd `/models` id (file path / alias) that differs
from the name the chat endpoint actually accepts (which already works).

### LinkedIn's own match assessment (`li_match`, free signal) — REQUIRES the pane view
LinkedIn shows a free profile/resume-match sentence (e.g. "Your profile and resume
match the required qualifications well." / "Your profile is missing required
qualifications."). **CRITICAL: it ONLY renders in the search-results split-pane — the
right side you get by SINGLE-CLICKING a card in the left list. It does NOT appear on the
standalone `/jobs/view/<id>` page.** An earlier version opened each job's standalone URL
in a new tab and never got the assessment. So the LinkedIn scraper is now
**click-driven** (see below). Stored in `jobs.li_match` (TEXT, NULL if absent).
The card also **loads ASYNCHRONOUSLY (spinner 3–7s)**, so `_extract_from_pane` POLLS
`_PANE_EXTRACT_JS` every 500ms: breaks when the sentence appears, waits up to 9s while a
loader/spinner is present, bails early after a 2.5s grace window if nothing is loading.
Logs `[li-match] (Ns) <sentence>`.

### LinkedIn scraper is CLICK-DRIVEN (not standalone-page navigation)
Because the match assessment only exists in the split-pane, the scraper stays on the
**search results page** and single-clicks each job card to load the right pane, then
reads description + Easy Apply + `li_match` from THAT pane. Key pieces:
- `_scrape_pages_by_clicking()` is the merged collect+process loop (replaced the old
  two-phase "collect all hrefs, then open each standalone" model — that's gone because
  cards must be present in the DOM to click them).
- `_SCROLL_AND_READ_JS` scrolls the i-th list shell into view and reads its card info.
- `_click_card()` single-clicks the card's `a[href*="/jobs/view/"]` (SPA — no full
  navigation) and waits for `currentJobId=<id>` to appear in the URL.
- `_PANE_EXTRACT_JS` reads desc/easy/li_match **scoped to the right pane container**
  (`.jobs-search__job-details` etc.). Scoping is ESSENTIAL: the left results list has
  its own per-card "Easy Apply" badges and titles that would otherwise pollute
  detection. Easy Apply keys on the pane's `button.jobs-apply-button`.
- **Cost/stealth optimization:** existing jobs (checked via `get_job_id_status(url)`)
  are counted + term-linked but NOT clicked/re-opened — fewer interactions = faster and
  more human. Only genuinely new jobs get a click + pane read + (optional) scoring.
- Both `scrape_async` (bulk, term=None) and `_scrape_one_term_async` (per-term) call the
  same helper. `_fetch_description` (the old standalone-page fetcher) was removed. `upsert_job` backfills it onto existing rows that lack it (free, no API).
Surfaced as a sortable "LinkedIn Match" column with a colored badge (`_li_match_badge`:
well/several→green, some→yellow, missing→red) and a category filter
(`LI_MATCH_FILTERS` in models → `list_jobs(li_match=...)`) so the user can hunt for
strong matches. Also shown on the detail page.

### Two score columns: Claude vs local LLM (kept separate)
Claude scores live in `jobs.match_score`/`match_reason`; local-LLM scores live in
SEPARATE `jobs.local_score`/`local_reason` columns so both are visible side by side.
`set_match(job_id, score, reason, backend)` writes to the column pair for the backend.
`score_job(..., backend=None)` forces an engine (else uses the setting). "Already
scored" in `score_job_if_enabled` is now PER-BACKEND (a Claude-scored job can still be
local-scored and vice versa). Listings show sortable "Match (Claude)" + "Match (Local)"
columns; the detail page shows both bars with a re-score button per engine
(`/jobs/<id>/rescore` takes a `backend` form field).

### Bulk re-score (select rows → action bar)
The listings table is wrapped in `<form id="bulk-form">`; each row has a
`name="job_ids"` checkbox (+ a header select-all and a JS-updated "N selected" count).
The action bar posts the checked ids to `/jobs/bulk-rescore` with a `backend` choice
(Local LLM / Claude API) via `hx-include="#bulk-form"`. Because scoring many jobs is
slow, it runs in a **daemon thread** (`_run_bulk_rescore`, guarded by `_rescore_lock`)
with an HTMX poller (`_rescore_status.html` → `/jobs/rescore-status`, every 2s; done
block has no trigger so it self-stops) and a Stop button (`/jobs/bulk-rescore/stop`).
Bulk re-score uses `rescore_job(job_id, backend)` which BYPASSES the "already scored"
skip and the global on/off gate (the user explicitly asked for it). Network calls happen
OUTSIDE the lock (avoids the non-reentrant-lock deadlock class).

### Resume Matching
- Resume stored as plain text in `resume.txt` (gitignored; user pastes their own)
- `resume_matcher.py` sends a prompt to the Claude API containing the resume and the job description, asks for a score (0–100) and a 2–3 sentence rationale
- Score stored in `jobs.match_score`; rationale in `jobs.match_reason`
- The web UI renders the score as a horizontal bar: 0–39 red, 40–69 yellow, 70–100 green
- Matching runs automatically on ingest if `matching.auto_score_on_ingest: true`; can also be triggered manually per-listing from the detail view

### Career-Page URL Management UI
- Route: `GET /sources` — lists all rows in `career_page_sources`
- A form on that page accepts a company name + URL; HTMX `POST /sources/add` inserts the row and refreshes the list inline
- Toggle active/inactive and delete also available via HTMX

### Applied Checkbox
Toggled via HTMX `POST /jobs/<id>/toggle-applied` — no page reload needed.

### Security
Runs locally only. Bind Flask to `127.0.0.1`. No authentication needed.

---

## Setup, Environment & Running

Installation, `.env` variables, uv environment management, and run/CLI commands
have moved to **`SETUP.md`**. See that file for:
- Environment variables (`.env`: `ANTHROPIC_API_KEY`, `GOOGLE_CREDENTIALS_FILE`)
- Gmail OAuth setup walkthrough
- Python environment with uv (install, first-time setup, activation, adding packages)
- Running the app (first-time setup, starting the scheduler + web server, running
  individual scrapers, re-scoring)

Web UI runs at `http://127.0.0.1:5000`.

---

## Out of Scope (for now)

- Authentication / multi-user support
- Deployment to a remote server
- Auto-apply or email notifications
- Resume editing within the app (edit `resume.txt` directly)

---

## Design Decisions & Lessons Learned

This section records decisions made (and bugs solved) during development so the
reasoning is not lost. Read this before changing scraping, threading, or stats code.

### Scraping is fully manual — no scheduler
All automated/scheduled scraping was deliberately removed. There is **no APScheduler
job** running scrapers on an interval. Every scrape is triggered explicitly by the
user from the web UI ("Scrape now" buttons) or the CLI. Do **not** re-introduce
background/cron scraping. The `schedule.interval_hours` config and any scheduler code
are dead — rationale: the user wants full control over when LinkedIn is hit (ban risk)
and over when the paid Claude API is called.

### Search terms live in `search_terms.toml` (not `config.yaml`)
LinkedIn/Gmail search configuration was moved to a user-editable `search_terms.toml`
with `[linkedin]`, `[gmail]`, and `[blacklist]` sections. Read with `tomllib`, write
with `tomlkit` (preserves comments/formatting). `scrapers/_config.py` owns all
read/write helpers (`get_linkedin_terms`, `add_linkedin_term`, `remove_linkedin_term`,
`get_linkedin_defaults`, `is_blacklisted_title`). The `[blacklist] title_patterns` list
filters out LinkedIn navigation noise ("manage job alerts", "view all jobs", etc.)
**before** any job is scored — this protects the API budget.

### Company blacklist (global, all sources)
`[blacklist] companies` in `search_terms.toml` is a global list of junk/scam/"trap"
employers. `_config.py` owns `get_blacklist_companies`, `is_blacklisted_company`
(case-insensitive substring match, None-safe), `add_blacklist_company`,
`remove_blacklist_company` (tomlkit). **Every** scraper (Gmail, LinkedIn both loops,
career pages) checks `is_blacklisted_company(company)` and `continue`s with
`counts["blacklisted"] += 1` **before** upsert/scoring — so a blacklisted company never
hits the DB or the paid API. Managed from the LinkedIn Terms page
(`/company-blacklist/add` + `/delete`, HTMX, partial `_company_blacklist.html`). This is
separate from the per-listing `hidden` flag: blacklist = "never let this company in";
hidden = "I already saw this one row, tuck it away."

### Deduplication & cost control (critical — API costs money)
- `jobs.url` is the unique key. `upsert_job()` returns `(row_id, status)` where status
  is `new` / `existing` / `applied`.
- On re-encountering a URL we do **NOT** update `fetched_at` — the original scrape
  timestamp is preserved (user explicitly requested this for accurate sort-by-time).
- Existing jobs are **never re-sent to the Claude API**. Only `new` jobs with a
  description get scored. This is the single most important cost guard.
- Expired listings ("no longer accepting applications") are kept in the DB (flagged
  `expired=1`) precisely so we don't re-scrape and re-score them. Default list view
  hides expired.

### Per-term stats must never double-count — `term_jobs` association table
This was a user-caught correctness requirement. Per-term average match score and
"distinct jobs found" must not inflate when a term is re-run over jobs it already saw.
Solution:
- Table `term_jobs (term, job_id, UNIQUE(term, job_id))`. `link_term_job()` uses
  `INSERT OR IGNORE`, so each job is linked to a term **at most once**.
- `term_score_stats(term)` computes stats **live from the DB** via `JOIN jobs`, rather
  than from a running total. This is self-correcting: if a job is re-scored, the term's
  numbers update automatically. It returns stats **split by engine** —
  `{distinct_found, claude:{avg,median,low,high,scored}, local:{avg,median,low,high,scored}}`
  — read from `match_score` (Claude) and `local_score` (local) respectively. The LinkedIn
  Terms page renders these as a per-term two-row comparison grid (`_score_summary` builds
  each engine's numbers; `engine_row` macro renders each row; empty engine → "not scored
  yet"). `distinct_found` and the run-activity tallies stay engine-agnostic.
- **What stays cumulative (correctly):** the per-run new/existing/skipped/reviewed
  event tallies in `linkedin_runs.json`. These are *run-activity* history, not job
  counts — watching "existing" climb tells you a term is going stale. Only the
  avg/distinct metrics come from the dedup'd table.

### Threading: the non-reentrant lock deadlock (important)
Background scrapes run in daemon threads guarded by `threading.Lock` (NON-reentrant).
A hard-to-find bug caused scrapes to appear stuck on "Running…" forever AND silently
skipped writing stats. Root cause: `_term_log()` (which itself acquires
`_term_scrapes_lock`) was being called from *inside* a `with _term_scrapes_lock:`
block → self-deadlock. The `print()` ran (terminal showed "Done") but the lock
re-acquire hung, so the `finally` never cleared `running` and `_save_run()` never ran.
**Rule: never call `_term_log()` (or anything that takes the lock) while already
holding `_term_scrapes_lock`.** The misleading symptom (UI stuck) wrongly pointed at
the HTMX frontend for several rounds; the real tell was that stats were *also* missing.

### LinkedIn scraping resilience (Playwright)
- Use a **persistent context** (`linkedin_session/`); first run headed to log in, then
  reused. Headed (not headless) to reduce blocking.
- Job extraction is **JS-based via `a[href*="/jobs/view/"]`** links, not CSS class
  selectors (LinkedIn's logged-in DOM differs from logged-out and changes often).
- The results list is **virtualized/recycled** — only ~9 of 25 shells render links at
  once. We **index-walk each `<li>` shell, scrolling it into view** to force render,
  accumulating unique hrefs. Without this we only got ~7 of 25.
- **Pagination**: Phase 1 collects links across `&start=0,25,50…` pages until
  `max_results` or no new links gained; Phase 2 fetches/scores each. Without this we
  capped at 25 even when 50 were requested.
- Remote filter is `f_WT=2`; time filter is `f_TPR=r{seconds}`.
- Description extraction tries specific selectors, then `<main>`, then the largest text
  block; one-shot retry on `ERR_NAME_NOT_RESOLVED`; checks `stop_event` so Stop works.
- **Easy Apply detection**: while on the detail page, `_fetch_description()` also runs
  `_EASY_APPLY_JS` and returns `(description, easy_apply)`. Stored in `jobs.easy_apply`
  (1/0). Only LinkedIn sets it; other sources default 0. Surfaced as a sortable "Easy
  Apply" column on the listings page so quick-apply jobs can be batched to the top.
  **Must be scoped by PROXIMITY, NOT the whole page** — v1 scanned all buttons/links
  and got false positives from the "More jobs for you" / "Similar jobs" cards at the
  page bottom (they carry their own Easy Apply badges). v2 keyed strictly on
  `button.jobs-apply-button` and over-corrected to zero (class absent / not hydrated).
  v3 (current) finds the Y coordinate where the bottom "other jobs" section begins (via
  its `h2/h3` heading text) and counts an Easy Apply control only if it sits ABOVE that
  boundary; if no marker heading is found it guards with the top 55% of the page. It
  waits for the apply CTA to hydrate first and logs a `[easy]` diagnostic line per job.

### Gmail ingestion
- Parse the **HTML email body's anchors** — one job per link, using the link's own text
  as the title and a per-link snippet as the description. (Early versions made 7
  duplicate rows from one email and gave every job the same score because they shared
  the whole email body as description.)
- Use **real Gmail API message IDs** from a "Load emails" list. (Trying to derive an
  API ID from a pasted URL caused 400 errors and was abandoned.)
- `_clean_company()` strips salary noise (": up to", "$", …) from the company field.

### Timestamps are local, not UTC
All `datetime.now()` (local), never `datetime.now(timezone.utc)`. An earlier UTC choice
made timestamps appear 7h off; existing rows were migrated. Keep everything local so
sort-by-time matches the user's wall clock.

### HTMX polling pattern (status that stops cleanly)
`_term_scrape_status.html` is the canonical self-replacing poller: the **running** block
carries `hx-get` + `hx-trigger="every 2s"` + `hx-target="this"` +
`hx-swap="outerHTML"`; the **done** block has **no trigger**, so polling stops itself.
A stable wrapper `<div id="term-status-{slug}">` is the swap target so only its contents
churn. (Note: the deadlock above was NOT a frontend bug — don't chase the template when
a background thread silently dies.)

### Schema migrations
`db/models.py::_migrate()` applies idempotent `ALTER`/`CREATE IF NOT EXISTS` statements
in a try/except and runs **automatically on import**, so the app self-heals an older DB
without a manual `--init`. Add new columns/tables there.
