# JobScrape

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-web-000000?logo=flask&logoColor=white)
![HTMX](https://img.shields.io/badge/HTMX-frontend-3366CC)
![SQLite](https://img.shields.io/badge/SQLite-storage-003B57?logo=sqlite&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-scraping-2EAD33?logo=playwright&logoColor=white)
![LLM](https://img.shields.io/badge/AI-Claude%20%7C%20local%20LLM-D97757)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

A personal job-listing aggregator. It pulls listings from **Gmail**, **LinkedIn**, and
**company career pages** into a local SQLite database, then serves a lightweight web UI
for browsing, application-tracking, and **AI-powered resume matching** — using either the
Claude API or a local LLM running on your own machine.

Everything runs locally on `127.0.0.1`. No accounts, no server, no data leaves your
machine (except the calls you choose to make to the Claude API or your local model).

> ⚠️ Personal-use project. It automates your *own* LinkedIn/Gmail browsing. Scrape
> responsibly and at your own risk — LinkedIn's terms restrict automated access, and
> aggressive scraping can get an account limited. JobScrape is **manual-trigger only**
> (no background/scheduled scraping) precisely so you stay in control.

---

## Features

- **Three sources, one inbox**
  - **Gmail** — parses job-alert / recruiter emails via the Gmail API (one row per job link)
  - **LinkedIn** — click-driven Playwright scraping of search results (per-term, configurable)
  - **Career pages** — a user-maintained list of company job-board URLs (`httpx` + BeautifulSoup, Playwright fallback)
- **AI resume matching** — scores each listing 0–100 against your résumé with a red→yellow→green bar and a written rationale
  - Pick your engine: **Claude API** (paid) or a **local OpenAI-compatible LLM** (Ollama, LM Studio, llama.cpp, vLLM — free)
  - Adjustable feedback detail (brief / standard / detailed — names specific missing skills)
  - Both engines' scores are kept in **separate columns** so you can compare them
- **Application tracking** — per-listing checkboxes for *Applied*, *Response received*, *Listing expired*, and *Hidden* (junk/scam), all via HTMX (no page reloads)
- **LinkedIn extras** — captures the **Easy Apply** flag and LinkedIn's own free **profile-match assessment**, both sortable/filterable
- **Bulk re-score** — select multiple rows and re-score them against either engine in the background, with live progress
- **Per-term stats** — dedup-safe average / median / min / max match scores per LinkedIn search term, so you learn which terms surface the best matches
- **Company blacklist** — globally drop junk/scam employers *before* they hit the DB or the paid API
- **Cost controls** — a global scoring kill-switch and strict deduplication so the paid API is never called twice for the same listing

---

## Engineering highlights

Some of the more interesting problems solved in this project (the *why* behind the code,
not just the *what*):

- **Resilient LinkedIn scraping.** LinkedIn's logged-in results list is a virtualized,
  recycled DOM that only renders ~9 of 25 cards at once, so extraction index-walks each
  list shell and scrolls it into view to force a render, then paginates via `&start=N`.
  The scraper is **click-driven**: it single-clicks each card to load the split-pane
  detail view (the only place LinkedIn surfaces its free profile-match assessment) and
  reads the description, Easy-Apply flag, and match text from that pane — scoping every
  query to the pane to avoid false positives from the "similar jobs" cards.
- **Cost-aware AI integration.** Strict URL-based deduplication guarantees the paid API
  is never called twice for the same listing; a global kill-switch and per-backend
  "already scored" checks give fine-grained control. The match assessment LinkedIn
  provides for free is captured and stored so it can be used *instead of* paid scoring.
- **Pluggable scoring backend.** The same prompt is dispatched to either the Claude API
  or any local OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM). Local model
  discovery, connection testing, and tolerant JSON parsing (local models are chattier)
  are all handled. Claude and local scores are stored in separate columns for comparison.
- **Concurrency done carefully.** Long-running scrapes and bulk re-scoring run in daemon
  threads with HTMX status pollers that stop themselves cleanly; shared state is guarded
  by locks with a deliberate no-reentrant-lock discipline (a self-deadlock bug and its
  fix are documented in `CLAUDE.md`).
- **Correct-by-construction stats.** Per-term match metrics use an association table with
  a `UNIQUE(term, job_id)` constraint and are computed live from the DB, so re-running a
  search term can never double-count a listing it already saw.
- **Zero-build frontend.** Vanilla HTML/CSS + HTMX gives instant inline interactivity
  (checkboxes, filters, live progress) with no JavaScript build pipeline.
- **Self-healing schema.** Idempotent migrations run automatically on import, so the
  database upgrades itself without manual steps.

> 📒 `CLAUDE.md` contains a detailed engineering log — design decisions, bugs hit, and the
> reasoning behind each fix — if you want to see how the project evolved.

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Database | SQLite (`sqlite3` stdlib) — single local file |
| Web | Flask + HTMX + vanilla CSS (no build step) |
| LinkedIn | Playwright (persistent browser context) |
| Career pages | `httpx` + BeautifulSoup, Playwright fallback |
| Gmail | Google Gmail API (OAuth2, read-only) |
| AI matching | Claude API (`anthropic`) **or** any OpenAI-compatible local LLM |
| Env / deps | [uv](https://docs.astral.sh/uv/) |

---

## Project structure

```
JobScrape/
├── run.py                     # entry point — starts the Flask app
├── scrapers/
│   ├── gmail_scraper.py       # Gmail API ingestion (HTML-anchor parsing)
│   ├── linkedin_scraper.py    # click-driven Playwright scraping
│   ├── career_page_scraper.py # httpx + BeautifulSoup, Playwright fallback
│   └── _config.py             # search_terms.toml read/write (tomllib + tomlkit)
├── matching/
│   └── resume_matcher.py      # Claude API + local-LLM scoring, prompt building
├── db/
│   ├── models.py              # thin SQLite layer (no ORM) + auto-migrations
│   └── schema.sql             # table DDL
├── web/
│   ├── app.py                 # Flask routes, background-task orchestration
│   ├── templates/             # Jinja + HTMX partials
│   └── static/style.css
├── app_settings.py            # runtime settings persistence (settings.toml)
├── CLAUDE.md                  # engineering log / design decisions
└── SETUP.md                   # full setup & operations guide
```

---

## Quick start

Full setup (Gmail OAuth, uv, running) is in **[SETUP.md](SETUP.md)**. The short version:

```bash
# 1. Install deps
uv venv
uv pip install -r requirements.txt
uv run playwright install chromium

# 2. Create the database
uv run python db/models.py --init

# 3. Configure (copy the templates, then edit them)
cp .env.example .env                          # add ANTHROPIC_API_KEY (only if using Claude)
cp search_terms.example.toml search_terms.toml # your LinkedIn/Gmail terms, location, blacklist
#  …and put your résumé in resume.txt

# 4. One-time logins
uv run python -m scrapers.gmail_scraper --auth-only        # Gmail OAuth consent
uv run python -m scrapers.linkedin_scraper --save-session  # log into LinkedIn (headed)

# 5. Run it
uv run python run.py        # → http://127.0.0.1:5000
```

On Windows you can also double-click `start_jobscrape.bat`.

---

## Configuration

- **`search_terms.toml`** — your LinkedIn search terms, location, Gmail keywords, and the
  title/company blacklist. Copy it from `search_terms.example.toml`. *(gitignored — stays private.)*
- **`config.yaml`** — default lookback windows.
- **Scoring settings** live in `settings.toml`, managed from the **Run Scrapers** page in the
  UI (engine, local LLM endpoint/model, feedback detail, on/off). *(gitignored.)*
- **`resume.txt`** — your plain-text résumé, used for matching. *(gitignored.)*

### Using a local LLM (free scoring)

Run any OpenAI-compatible server (e.g. Ollama: `ollama serve`), then in the UI under
**Run Scrapers → Scoring engine** choose **Local LLM**, enter the base URL
(e.g. `http://localhost:11434/v1`), click **Detect model**, and **Save**. Scoring then
costs nothing.

---

## Privacy

This repo is safe to be public: all personal and sensitive files are gitignored and never
committed —

```
.env  credentials.json  token.json      # secrets
resume.txt  resume_old*.txt             # your résumé(s)
search_terms.toml                       # your terms, location, blacklist
settings.toml  app_settings.json        # local UI settings
jobscrape.db*  linkedin_session/        # your scraped data & login session
```

The committed `*.example.*` files show the expected format without any of your data.

---

## License

Released under the [MIT License](LICENSE).

---

## Not affiliated

JobScrape is an independent personal tool and is not affiliated with or endorsed by
LinkedIn, Google, or Anthropic. Use it in accordance with each service's terms.
