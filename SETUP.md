# JobScrape — Setup & Operations

Installation, environment, credentials, and run instructions for JobScrape.
For project goals, architecture, schema, and design decisions, see `CLAUDE.md`.

---

## Configuration (`config.yaml`)

> Note: LinkedIn/Gmail search terms and the title blacklist are now primarily managed
> in `search_terms.toml` (see `CLAUDE.md` → Design Decisions). `config.yaml` holds the
> remaining settings shown below.

```yaml
schedule:
  interval_hours: 6          # how often scrapers run

linkedin:
  search_terms:
    - "software engineer"
    - "backend developer"
  location: "Copenhagen, Denmark"
  max_results_per_term: 50

gmail:
  label_filter: ""           # optional Gmail label to restrict search
  subject_keywords:
    - "job alert"
    - "new job"
    - "hiring"

career_pages:                # also manageable via the web UI /sources page
  - company: "Acme Corp"
    url: "https://acme.com/careers"

matching:
  enabled: true
  model: "claude-opus-4-5"  # Claude model to use for resume matching
  auto_score_on_ingest: true # score new listings automatically when scraped
```

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CREDENTIALS_FILE=credentials.json   # path to Gmail OAuth credentials
```

---

## Gmail OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a new project (e.g. "JobScrape")
2. Enable the **Gmail API** (APIs & Services → Library → search "Gmail API")
3. Go to **APIs & Services → OAuth consent screen** → External → fill in app name and your email
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the resulting `credentials.json` into the project root
5. Add your Google account as a test user (OAuth consent screen → Test users)
6. First run of `gmail_scraper.py` opens a browser tab for one-time consent; the token is saved to `token.json` (gitignored) and reused automatically

Scope needed: `https://www.googleapis.com/auth/gmail.readonly`

---

## Python Environment (uv)

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management.
Install uv once (if not already installed):
```bash
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### First-time setup
```bash
uv venv                        # creates .venv in the project root
uv pip install -r requirements.txt
```

### Activating the environment (optional — uv run works without it)
```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### Running commands
Prefix any command with `uv run` to use the project's virtual environment without activating it:
```bash
uv run python db/models.py --init
uv run python run.py
uv run python -m scrapers.gmail_scraper
```

### Adding / removing packages
```bash
uv pip install <package>           # add a package
uv pip uninstall <package>         # remove a package
uv pip freeze > requirements.txt   # update the pinned requirements file
```

---

## Running the App

```bash
# First time setup
uv venv
uv pip install -r requirements.txt
uv run python db/models.py --init        # creates jobscrape.db from schema.sql
uv run playwright install chromium       # download Playwright browser

# One-time Gmail OAuth consent (opens browser)
uv run python -m scrapers.gmail_scraper --auth-only

# One-time LinkedIn login (opens headed browser; log in manually)
uv run python -m scrapers.linkedin_scraper --save-session

# Start everything (scheduler + web server)
uv run python run.py

# Run a single scraper manually
uv run python -m scrapers.gmail_scraper
uv run python -m scrapers.linkedin_scraper
uv run python -m scrapers.career_page_scraper

# Re-score all unscored listings
uv run python -m matching.resume_matcher --score-all
```

Web UI available at: `http://127.0.0.1:5000`
