"""
Thin database wrapper — raw sqlite3, no ORM.
Run directly to initialise the database:
    python db/models.py --init
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "jobscrape.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    schema = SCHEMA_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(schema)
    _migrate()
    print(f"Database initialised at {DB_PATH}")


def _migrate() -> None:
    """Safely apply any schema additions to an existing database."""
    migrations = [
        "ALTER TABLE jobs ADD COLUMN response_received INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN expired INTEGER NOT NULL DEFAULT 0",
        # hidden: junk/scam/broken listings the user dismissed. Kept in the DB (so
        # dedup still treats their URLs as 'existing' and never re-scores them) but
        # filtered out of the default list view.
        "ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
        # easy_apply: 1 if the LinkedIn listing has the "Easy Apply" button (quick
        # one-click apply). 0 = no / unknown (non-LinkedIn sources are always 0).
        "ALTER TABLE jobs ADD COLUMN easy_apply INTEGER NOT NULL DEFAULT 0",
        # li_match: LinkedIn's OWN free profile/resume-match sentence shown in the
        # detail pane (e.g. "Your profile and resume match the required qualifications
        # well."). NULL = not present / not LinkedIn. This is free (no API cost).
        "ALTER TABLE jobs ADD COLUMN li_match TEXT",
        # local_score/local_reason: score from a LOCAL LLM, kept SEPARATE from the
        # Claude API score (match_score/match_reason) so both can be compared side by
        # side. NULL until scored by the local backend.
        "ALTER TABLE jobs ADD COLUMN local_score INTEGER",
        "ALTER TABLE jobs ADD COLUMN local_reason TEXT",
        # Association of which search term surfaced which job (for per-term stats).
        # UNIQUE(term, job_id) means each job counts at most ONCE per term.
        """CREATE TABLE IF NOT EXISTS term_jobs (
            term    TEXT    NOT NULL,
            job_id  INTEGER NOT NULL,
            UNIQUE(term, job_id)
        )""",
    ]
    with get_conn() as conn:
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column/table already exists


# Run migrations automatically on every import so the app never needs --init
_migrate()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

# Status constants returned by upsert_job
STATUS_NEW      = "new"       # first time we've seen this URL
STATUS_EXISTING = "existing"  # URL already in DB, not yet applied
STATUS_APPLIED  = "applied"   # URL already in DB and marked as applied

def upsert_job(
    source: str,
    company: str | None,
    title: str | None,
    location: str | None,
    url: str,
    description: str | None,
    posted_at: str | None,
    fetched_at: str,
    easy_apply: int = 0,
    li_match: str | None = None,
) -> tuple[int, str]:
    """
    Insert a job or update fetched_at if the URL already exists.
    Returns (row_id, status) where status is one of STATUS_NEW / STATUS_EXISTING / STATUS_APPLIED.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, applied FROM jobs WHERE url = ?", (url,)
        ).fetchone()

        if existing:
            # Leave fetched_at unchanged — preserve the original scrape timestamp.
            # Backfill li_match if we now have it and the row didn't (free, no API).
            if li_match:
                conn.execute(
                    "UPDATE jobs SET li_match = ? WHERE id = ? AND (li_match IS NULL OR li_match = '')",
                    (li_match, existing["id"]),
                )
            status = STATUS_APPLIED if existing["applied"] else STATUS_EXISTING
            return existing["id"], status

        cur = conn.execute(
            """INSERT INTO jobs
               (source, company, title, location, url, description, posted_at, fetched_at, easy_apply, li_match)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, company, title, location, url, description, posted_at, fetched_at, easy_apply, li_match)
        )
        return cur.lastrowid, STATUS_NEW


def get_job(job_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_id_status(url: str) -> tuple[int, str] | None:
    """Cheap existence check by URL (no insert). Returns (id, status) where status
    is STATUS_APPLIED/STATUS_EXISTING, or None if the URL is new. Lets scrapers skip
    re-opening jobs they already have."""
    with get_conn() as conn:
        row = conn.execute("SELECT id, applied FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    return row["id"], (STATUS_APPLIED if row["applied"] else STATUS_EXISTING)


# LinkedIn match-filter categories → SQL fragment over the li_match text.
LI_MATCH_FILTERS = {
    "well":    "li_match LIKE '%qualifications well%'",
    "several": "li_match LIKE '%several%qualification%'",
    "some":    "li_match LIKE '%some required qualification%'",
    "missing": "li_match LIKE '%is missing%'",
    "any":     "(li_match IS NOT NULL AND li_match != '')",
    "none":    "(li_match IS NULL OR li_match = '')",
}


def list_jobs(
    applied: int | None = None,
    response_received: int | None = None,
    expired: int | None = 0,      # default: hide expired listings
    hidden: int | None = 0,       # default: hide dismissed (junk/scam) listings
    min_score: int | None = None,
    li_match: str | None = None,  # category key from LI_MATCH_FILTERS
    order_by: str = "fetched_at DESC",
) -> list[sqlite3.Row]:
    clauses = []
    params: list = []
    if applied is not None:
        clauses.append("applied = ?")
        params.append(applied)
    if response_received is not None:
        clauses.append("response_received = ?")
        params.append(response_received)
    if expired is not None:
        clauses.append("expired = ?")
        params.append(expired)
    if hidden is not None:
        clauses.append("hidden = ?")
        params.append(hidden)
    if min_score is not None:
        clauses.append("match_score >= ?")
        params.append(min_score)
    if li_match in LI_MATCH_FILTERS:
        clauses.append(LI_MATCH_FILTERS[li_match])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    # Sorting on li_match should rank by ASSESSMENT QUALITY, not alphabetically.
    # well (4) > several (3) > some (2) > missing (1) > none/NULL (0).
    if order_by.lower().startswith("li_match"):
        direction = "DESC" if "desc" in order_by.lower() else "ASC"
        rank = (
            "CASE"
            " WHEN li_match LIKE '%qualifications well%' THEN 4"
            " WHEN li_match LIKE '%several%qualification%' THEN 3"
            " WHEN li_match LIKE '%some required qualification%' THEN 2"
            " WHEN li_match LIKE '%is missing%' THEN 1"
            " ELSE 0 END"
        )
        order_by = f"({rank}) {direction}, match_score DESC, fetched_at DESC"

    sql = f"SELECT * FROM jobs {where} ORDER BY {order_by}"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Term ↔ job associations (per-term stats, dedup-safe)
# ---------------------------------------------------------------------------

def link_term_job(term: str, job_id: int) -> None:
    """Record that `term` surfaced `job_id`. INSERT OR IGNORE → each job counts
    at most once per term, so re-runs never double-count."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO term_jobs (term, job_id) VALUES (?, ?)",
            (term, job_id),
        )


def term_score_stats(term: str) -> dict:
    """
    Compute live, dedup-safe stats for a term from the database:
      - distinct_found: distinct jobs this term has ever surfaced
      - scored_count:   how many of those have a match score
      - avg_score:      average match score over distinct scored jobs (or None)
      - median_score:   median match score over distinct scored jobs (or None)
      - min_score:      lowest match score (or None)
      - max_score:      highest match score (or None)

    All metrics are computed over DISTINCT jobs (via the term_jobs association table),
    so re-running a term never double-counts a job it already surfaced.
    """
    with get_conn() as conn:
        # distinct count (scored or not)
        distinct_found = conn.execute(
            "SELECT COUNT(*) AS c FROM term_jobs WHERE term = ?", (term,)
        ).fetchone()["c"]
        # the actual scores, so we can compute median in Python
        rows = conn.execute(
            """
            SELECT j.match_score AS s
            FROM term_jobs tj
            JOIN jobs j ON j.id = tj.job_id
            WHERE tj.term = ? AND j.match_score IS NOT NULL
            ORDER BY j.match_score
            """,
            (term,),
        ).fetchall()

    scores = [r["s"] for r in rows]
    scored_count = len(scores)

    if scored_count == 0:
        return {
            "distinct_found": distinct_found or 0,
            "scored_count":   0,
            "avg_score":      None,
            "median_score":   None,
            "min_score":      None,
            "max_score":      None,
        }

    # median of the sorted list
    mid = scored_count // 2
    if scored_count % 2:
        median = float(scores[mid])
    else:
        median = (scores[mid - 1] + scores[mid]) / 2

    return {
        "distinct_found": distinct_found or 0,
        "scored_count":   scored_count,
        "avg_score":      sum(scores) / scored_count,
        "median_score":   median,
        "min_score":      scores[0],
        "max_score":      scores[-1],
    }


def toggle_applied(job_id: int) -> int:
    """Flip applied 0↔1. Returns the new value."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET applied = 1 - applied WHERE id = ?", (job_id,)
        )
        row = conn.execute("SELECT applied FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["applied"]


def toggle_expired(job_id: int) -> int:
    """Flip expired 0↔1. Returns the new value."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET expired = 1 - expired WHERE id = ?", (job_id,)
        )
        row = conn.execute("SELECT expired FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["expired"]


def toggle_hidden(job_id: int) -> int:
    """Flip hidden 0↔1. Returns the new value. Row stays in the DB either way."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET hidden = 1 - hidden WHERE id = ?", (job_id,)
        )
        row = conn.execute("SELECT hidden FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["hidden"]


def toggle_response_received(job_id: int) -> int:
    """Flip response_received 0↔1. Returns the new value."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET response_received = 1 - response_received WHERE id = ?", (job_id,)
        )
        row = conn.execute("SELECT response_received FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["response_received"]


def update_location(job_id: int, location: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET location = ? WHERE id = ?", (location or None, job_id))


def set_match(job_id: int, score: int, reason: str, backend: str = "claude") -> None:
    """Store a score+reason in the column pair for the given backend:
    backend='local' → local_score/local_reason; otherwise match_score/match_reason."""
    if backend == "local":
        score_col, reason_col = "local_score", "local_reason"
    else:
        score_col, reason_col = "match_score", "match_reason"
    with get_conn() as conn:
        conn.execute(
            f"UPDATE jobs SET {score_col} = ?, {reason_col} = ? WHERE id = ?",
            (score, reason, job_id),
        )


def get_unscored_jobs() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE match_score IS NULL AND description IS NOT NULL"
        ).fetchall()


def update_notes(job_id: int, notes: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))


# ---------------------------------------------------------------------------
# Career page sources
# ---------------------------------------------------------------------------

def list_sources(active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM career_page_sources"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY company"
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def add_source(company: str, url: str, selector: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO career_page_sources (company, url, selector) VALUES (?, ?, ?)",
            (company, url, selector),
        )
        return cur.lastrowid


def toggle_source_active(source_id: int) -> int:
    with get_conn() as conn:
        conn.execute(
            "UPDATE career_page_sources SET active = 1 - active WHERE id = ?",
            (source_id,),
        )
        row = conn.execute(
            "SELECT active FROM career_page_sources WHERE id = ?", (source_id,)
        ).fetchone()
        return row["active"]


def delete_source(source_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM career_page_sources WHERE id = ?", (source_id,))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--init" in sys.argv:
        init_db()
    else:
        print("Usage: python db/models.py --init")
