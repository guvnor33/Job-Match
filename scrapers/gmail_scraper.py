"""
Gmail scraper — reads job-related emails via the Gmail API and ingests them as job listings.

For digest emails (e.g. LinkedIn job alerts) it parses the HTML body to extract each
individual job link with its anchor text as the title and a surrounding context snippet
as the description — so each listing gets its own AI match score.

First-time setup:
    python -m scrapers.gmail_scraper --auth-only

Normal run:
    python -m scrapers.gmail_scraper
"""
import base64
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

ROOT = Path(__file__).parent.parent
CREDENTIALS_FILE = ROOT / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = ROOT / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Minimum anchor-text length to be considered a job title (filters out "Click here", "Apply", etc.)
MIN_TITLE_LEN = 8


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Body decoding
# ---------------------------------------------------------------------------

def _decode_part(payload: dict, mime_type: str) -> str:
    """Recursively find and decode a MIME part by type (text/plain or text/html)."""
    if payload.get("mimeType", "") == mime_type:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _decode_part(part, mime_type)
        if result:
            return result
    return ""


# ---------------------------------------------------------------------------
# URL / link helpers
# ---------------------------------------------------------------------------

_NOISE_PATTERNS = [
    "unsubscribe", "optout", "opt-out", "manage-email", "email-settings",
    "twitter.com", "facebook.com", "instagram.com", "pixel", "beacon",
    "open.aspx", "open.php",
]

_JOB_SIGNALS = ["job", "career", "position", "apply", "posting", "vacancy", "role", "opening"]

_SEARCH_RESULT_PATTERNS = ["search-result", "search_result", "jobs/search", "jobs/collections"]


def _is_noise_url(url: str) -> bool:
    lower = url.lower()
    return any(n in lower for n in _NOISE_PATTERNS)


def _is_job_url(url: str) -> bool:
    if _is_noise_url(url):
        return False
    lower = url.lower()
    return any(s in lower for s in _JOB_SIGNALS)


def _is_search_results_url(url: str) -> bool:
    lower = url.lower()
    return any(p in lower for p in _SEARCH_RESULT_PATTERNS)


def _is_individual_job_url(url: str) -> bool:
    """True if the URL points to a specific job listing rather than a search page."""
    if _is_search_results_url(url):
        return False
    lower = url.lower()
    return any(s in lower for s in ["/jobs/view/", "/job/", "apply", "posting", "requisition", "vacancy"])


# ---------------------------------------------------------------------------
# HTML job extraction (for digest emails)
# ---------------------------------------------------------------------------

def _clean_title(raw: str) -> str:
    """
    Strip company/location/salary noise that LinkedIn bundles into anchor text.
    e.g. "Software Engineer – Backend | Remote  Crossing Hurdles · United States $60-120/hr Easy Apply"
      → "Software Engineer – Backend"
    """
    for sep in [" | ", " · ", "\n", " — ", "  "]:
        if sep in raw:
            raw = raw.split(sep)[0]
    return raw.strip()


def _clean_company(raw: str | None) -> str | None:
    """
    Strip salary, rates, and other noise appended to company names.
    e.g. "Recruiting from Scratch: up to $129K/year" → "Recruiting from Scratch"
    """
    if not raw:
        return None
    # Strip at known noise markers
    for sep in [": up to", ": from", " up to", " - $", " – $", "$", "  ", "\n"]:
        if sep in raw:
            raw = raw.split(sep)[0]
    # Also strip anything after a colon that looks like salary info
    m = re.match(r"^([^:]+?):\s*\$", raw)
    if m:
        raw = m.group(1)
    return raw.strip() or None


def _canonical_url(url: str) -> str:
    """
    Strip tracking query parameters so the same job from two emails shares one URL.
    Keeps the path (which contains the stable job ID) and drops everything after '?'.
    """
    return url.split("?")[0].rstrip("/")


def _extract_jobs_from_html(html: str, fallback_company: str | None, log_fn=None) -> list[dict]:
    """
    Parse an HTML email body and return a list of job dicts:
        {url, title, company, description}

    Strategy:
    - Find all <a> tags whose href is a job URL
    - Clean the anchor text to extract just the job title
    - Walk up the DOM to collect surrounding context as a per-job description snippet
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    blacklisted = 0
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = _canonical_url(a["href"].strip())
        raw_title = a.get_text(separator=" ", strip=True)

        # Basic filters
        if not href or not raw_title:
            continue
        if len(raw_title) < MIN_TITLE_LEN:
            continue
        if not _is_job_url(href):
            continue
        if _is_search_results_url(href):
            continue
        if href in seen_urls:
            continue

        title = _clean_title(raw_title)

        # Drop navigation links, UI chrome, and anything on the blacklist
        from scrapers._config import is_blacklisted_title
        if is_blacklisted_title(title):
            if log_fn:
                log_fn(f"    [SKIP]     {title!r} — blacklisted")
            blacklisted += 1
            continue

        # Collect context text by walking up to the nearest "block" ancestor
        # that contains meaningful surrounding info (company name, location, snippet)
        context_text = raw_title
        node = a.parent
        for _ in range(5):
            if node is None:
                break
            candidate = node.get_text(separator=" ", strip=True)
            if len(candidate) > len(context_text) + 10 and len(candidate) < 2000:
                context_text = candidate
            if len(candidate) > 600:
                break
            node = node.parent

        seen_urls.add(href)
        jobs.append({
            "url": href,
            "title": title,
            "company": fallback_company,
            "description": context_text,
        })

    return jobs, blacklisted


# ---------------------------------------------------------------------------
# Subject parsing (used when HTML extraction finds nothing)
# ---------------------------------------------------------------------------

def _parse_subject(subject: str) -> tuple[str, str | None]:
    """
    Extract (title, company) from common email subject formats:
      - "Company is hiring for a Role"
      - "Role at Company"
      - "Job Alert: Role - Company"
    """
    subject = re.sub(
        r"^(re:|fw:|fwd:|job alert[:\-–]?\s*|new job[:\-–]?\s*|hiring alert[:\-–]?\s*)",
        "", subject, flags=re.I
    ).strip()

    # "Company is hiring (for) (a/an) Role"
    m = re.search(r"^(.+?)\s+is\s+hiring\s+(?:for\s+)?(?:an?\s+)?(.+)", subject, re.I)
    if m:
        return m.group(2).strip(), _clean_company(m.group(1))

    # "Role at/@ Company"
    m = re.search(r"^(.+?)\s+(?:at|@)\s+(.+)", subject, re.I)
    if m:
        return m.group(1).strip(), _clean_company(m.group(2))

    # "Role - Company" or "Role – Company"
    m = re.search(r"^(.+?)\s*[-–]\s*(.+)", subject)
    if m:
        return m.group(1).strip(), _clean_company(m.group(2))

    return subject, None


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def _build_query(yaml_cfg: dict, lookback_days: int | None = None) -> str:
    from scrapers._config import load_search_config
    toml_cfg = load_search_config()

    keywords = toml_cfg.get("gmail", {}).get("subject_keywords", [])
    label = toml_cfg.get("gmail", {}).get("label_filter", "") or ""
    days = lookback_days if lookback_days is not None else yaml_cfg.get("gmail", {}).get("lookback_days", 7)

    subject_parts = " OR ".join(f'subject:"{kw}"' for kw in keywords)
    query = f"({subject_parts})" if subject_parts else ""
    if label:
        query = f"label:{label} {query}".strip()

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"{query} after:{cutoff}".strip()
    return query or "subject:job"


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def scrape(service=None, dry_run: bool = False, lookback_days: int | None = None,
           stop_event=None, log_fn=None) -> dict:
    """Fetch job emails and store them. Returns a breakdown dict."""
    from db.models import upsert_job, STATUS_NEW, STATUS_EXISTING, STATUS_APPLIED
    from matching.resume_matcher import score_job_if_enabled
    log = log_fn or print

    cfg = _load_config()
    if service is None:
        service = get_gmail_service()

    query = _build_query(cfg, lookback_days=lookback_days)
    log(f"  Query: {query}")

    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])
    log(f"  {len(messages)} matching email(s) found")

    counts = {"new": 0, "scored": 0, "existing": 0, "reviewed": 0, "blacklisted": 0}
    now = datetime.now().isoformat()

    for msg_ref in messages:
        if stop_event and stop_event.is_set():
            log("  Stop requested, halting.")
            break
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("subject", "")
        date_str = headers.get("date", "")

        _, fallback_company = _parse_subject(subject)

        html_body = _decode_part(msg["payload"], "text/html")
        jobs, n_blacklisted = _extract_jobs_from_html(html_body, fallback_company, log_fn=log) \
            if html_body else ([], 0)
        counts["blacklisted"] += n_blacklisted

        if not jobs:
            plain_body = _decode_part(msg["payload"], "text/plain")
            title, company = _parse_subject(subject)
            jobs = [{
                "url": f"gmail:{msg_ref['id']}",
                "title": title,
                "company": company,
                "description": plain_body[:4000],
            }]

        log(f"  Email: {subject!r} — {len(jobs)} listing(s)")

        from scrapers._config import is_blacklisted_company
        for job in jobs:
            label = f"{job['title'] or '?'}" + (f" @ {job['company']}" if job['company'] else "")

            if is_blacklisted_company(job["company"]):
                log(f"    [SKIP]     {job['company']!r} — blacklisted company")
                counts["blacklisted"] += 1
                continue

            if dry_run:
                log(f"    [dry] {label}")
                counts["new"] += 1
                continue

            job_id, status = upsert_job(
                source="gmail",
                company=job["company"],
                title=job["title"],
                location=None,
                url=job["url"],
                description=job["description"],
                posted_at=date_str,
                fetched_at=now,
            )

            if status == STATUS_NEW:
                score_status = score_job_if_enabled(job_id)
                if score_status.startswith("scored"):
                    counts["scored"] += 1
                counts["new"] += 1
                log(f"    [NEW]      {label} — {score_status}")
            elif status == STATUS_APPLIED:
                counts["reviewed"] += 1
                log(f"    [REVIEWED] {label} — already marked as applied, skipping")
            else:
                counts["existing"] += 1
                log(f"    [EXISTS]   {label} — already in database, skipping")

    return counts


def _load_config():
    import yaml
    return yaml.safe_load((ROOT / "config.yaml").read_text())


# ---------------------------------------------------------------------------
# Single-email scrape (from Gmail web URL)
# ---------------------------------------------------------------------------

def _extract_id_from_gmail_url(gmail_url: str) -> str:
    """
    Pull the message/thread ID out of a Gmail web URL.
    Handles formats like:
      https://mail.google.com/mail/u/0/#all/FMfcgzQgMCZWhzjvRbRnTsLvXSstwttC
      https://mail.google.com/mail/u/0/#inbox/FMfcgzQgMCZWhzjvRbRnTsLvXSstwttC
    """
    m = re.search(r"#[^/]+/([A-Za-z0-9_\-]+)$", gmail_url.strip())
    if not m:
        raise ValueError(f"Could not find a message ID in: {gmail_url!r}")
    return m.group(1)


def _decode_gmail_url_id(url_id: str) -> list[str]:
    """
    Gmail web URL IDs (e.g. FMfcgzQgMCZWhzjvRbRnTsLvXSstwttC) are base64url-encoded
    byte sequences. The Gmail API uses 16-char hex IDs (8 bytes each).
    The web ID encodes three packed 8-byte values: [thread_id][msg_id][???].
    We try each 8-byte chunk as a candidate API ID.
    """
    import base64
    candidates = []
    try:
        padded = url_id + "=" * (-len(url_id) % 4)
        raw = base64.urlsafe_b64decode(padded)
        # Split into 8-byte chunks — each is a potential API ID
        for i in range(0, len(raw), 8):
            chunk = raw[i:i + 8]
            if len(chunk) == 8:
                candidates.append(chunk.hex())
    except Exception:
        pass
    return candidates


def _fetch_message(service, url_id: str) -> dict:
    """
    Fetch a Gmail message payload by its web URL ID.
    Tries the raw URL ID first, then each 8-byte chunk decoded from the base64url ID
    against both the messages and threads endpoints.
    """
    # Build ordered list of ID candidates to try
    candidates = [url_id] + _decode_gmail_url_id(url_id)

    last_error = None
    for candidate in candidates:
        # Try as message ID
        try:
            return service.users().messages().get(
                userId="me", id=candidate, format="full"
            ).execute()
        except Exception as e:
            last_error = e

        # Try as thread ID — return the most recent message in the thread
        try:
            thread = service.users().threads().get(
                userId="me", id=candidate, format="full"
            ).execute()
            msgs = thread.get("messages", [])
            if msgs:
                return msgs[-1]
        except Exception as e:
            last_error = e

    raise ValueError(
        f"Could not fetch message with ID '{url_id}'. Last error: {last_error}"
    )


def list_recent_job_emails(lookback_days: int = 14, max_results: int = 30, service=None) -> list[dict]:
    """
    Return a list of recent job-related emails as dicts:
        {id, subject, sender, date}
    Uses the same search query as the main scraper.
    """
    cfg = _load_config()
    if service is None:
        service = get_gmail_service()

    query = _build_query(cfg, lookback_days=lookback_days)
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    emails = []
    for ref in results.get("messages", []):
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()
            hdrs = {h["name"].lower(): h["value"]
                    for h in msg["payload"].get("headers", [])}
            emails.append({
                "id":      ref["id"],
                "subject": hdrs.get("subject", "(no subject)"),
                "sender":  hdrs.get("from", ""),
                "date":    hdrs.get("date", ""),
            })
        except Exception:
            pass
    return emails


def scrape_message_by_id(message_id: str, service=None) -> tuple[int, list[str]]:
    """
    Fetch and process a single Gmail message by its API message ID.
    Returns (count_added, [list of job titles found]).
    """
    from db.models import upsert_job
    from matching.resume_matcher import score_job_if_enabled

    if service is None:
        service = get_gmail_service()

    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject  = headers.get("subject", "")
    date_str = headers.get("date", "")

    _, fallback_company = _parse_subject(subject)
    html_body = _decode_part(msg["payload"], "text/html")
    jobs, _ = _extract_jobs_from_html(html_body, fallback_company) if html_body else ([], 0)

    if not jobs:
        plain_body = _decode_part(msg["payload"], "text/plain")
        title, company = _parse_subject(subject)
        jobs = [{
            "url": f"gmail:{message_id}",
            "title": title,
            "company": company,
            "description": plain_body[:4000],
        }]

    print(f"  [gmail] {subject!r} → {len(jobs)} job(s) found")
    now = datetime.now().isoformat()
    titles = []

    for job in jobs:
        job_id = upsert_job(
            source="gmail",
            company=job["company"],
            title=job["title"],
            location=None,
            url=job["url"],
            description=job["description"],
            posted_at=date_str,
            fetched_at=now,
        )
        score_job_if_enabled(job_id)
        titles.append(job["title"])

    return len(jobs), titles


def scrape_from_url(gmail_url: str, service=None) -> tuple[int, list[str]]:
    """
    Fetch a single Gmail message by its web URL, extract jobs, store and score them.
    Returns (count_added, [list of job titles found]).
    """
    from db.models import upsert_job
    from matching.resume_matcher import score_job_if_enabled

    if service is None:
        service = get_gmail_service()

    url_id = _extract_id_from_gmail_url(gmail_url)
    print(f"[gmail] fetching message id={url_id!r}")

    msg = _fetch_message(service, url_id)

    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject  = headers.get("subject", "")
    date_str = headers.get("date", "")

    _, fallback_company = _parse_subject(subject)

    html_body = _decode_part(msg["payload"], "text/html")
    jobs, _ = _extract_jobs_from_html(html_body, fallback_company) if html_body else ([], 0)

    if not jobs:
        plain_body = _decode_part(msg["payload"], "text/plain")
        title, company = _parse_subject(subject)
        jobs = [{
            "url": f"gmail:{msg['id']}",
            "title": title,
            "company": company,
            "description": plain_body[:4000],
        }]

    print(f"  [gmail] {subject!r} → {len(jobs)} job(s) found")
    now = datetime.now().isoformat()
    titles = []

    for job in jobs:
        job_id = upsert_job(
            source="gmail",
            company=job["company"],
            title=job["title"],
            location=None,
            url=job["url"],
            description=job["description"],
            posted_at=date_str,
            fetched_at=now,
        )
        score_job_if_enabled(job_id)
        titles.append(job["title"])

    return len(jobs), titles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--auth-only" in sys.argv:
        get_gmail_service()
        print("Auth complete. token.json saved.")
    else:
        scrape(dry_run="--dry-run" in sys.argv)
