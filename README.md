# JobScrape

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

## Not affiliated

JobScrape is an independent personal tool and is not affiliated with or endorsed by
LinkedIn, Google, or Anthropic. Use it in accordance with each service's terms.
