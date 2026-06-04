CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    company       TEXT,
    title         TEXT,
    location      TEXT,
    url           TEXT UNIQUE,
    description   TEXT,
    posted_at     TEXT,
    fetched_at    TEXT NOT NULL,
    applied            INTEGER NOT NULL DEFAULT 0,
    response_received  INTEGER NOT NULL DEFAULT 0,
    expired            INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    match_score   INTEGER,
    match_reason  TEXT
);

CREATE TABLE IF NOT EXISTS career_page_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    url         TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,
    selector    TEXT
);
