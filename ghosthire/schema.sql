-- GhostHire schema. Hand-written DDL, no ORM: a reader can check the data
-- model against the queries without learning a framework first.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS job_listings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL,   -- 'internshala' | 'board_company' | 'career_page'
    source_company_slug     TEXT,            -- which company this row is filed under
    company_name            TEXT NOT NULL,
    company_name_normalized TEXT NOT NULL,
    job_title               TEXT NOT NULL,
    job_title_normalized    TEXT NOT NULL,
    location                TEXT,
    location_normalized     TEXT,
    date_posted             TEXT,            -- ISO, from the LISTING's own date
    -- 'exact' | 'relative' | 'absent'. A relative date is only accurate to its
    -- own unit, so any staleness claim has to declare which it stands on.
    date_posted_confidence  TEXT,
    job_url                 TEXT,
    salary_range            TEXT,
    first_seen              TEXT NOT NULL,   -- our archive; kept, never scored on
    last_seen               TEXT NOT NULL,
    observation_count       INTEGER DEFAULT 1,
    is_active               INTEGER DEFAULT 1,
    collector_id            TEXT,            -- c_* provenance on every row
    raw_json                TEXT,
    created_at              TEXT DEFAULT (datetime('now')),
    UNIQUE(source, job_url)
);

CREATE INDEX IF NOT EXISTS idx_listings_company
    ON job_listings(company_name_normalized);
CREATE INDEX IF NOT EXISTS idx_listings_source
    ON job_listings(source, source_company_slug);

CREATE TABLE IF NOT EXISTS ghost_scores (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id                INTEGER NOT NULL REFERENCES job_listings(id),
    -- NULL when career_page_checked = 0, and the CHECK below enforces it.
    -- A sentinel like -1 would eventually render in a UI as a real score; a
    -- NULL cannot be mistaken for a measurement.
    ghost_score               INTEGER,
    signals                   TEXT NOT NULL,  -- JSON array
    matched_career_listing_id INTEGER REFERENCES job_listings(id),
    match_confidence          REAL,           -- 0.0-1.0, shown in the UI
    -- 0 = we have no career data for this company. Such a listing MUST NOT be
    -- scored as a ghost: absence of evidence is not evidence of absence, and
    -- skipping this is the fastest way to publish a wrong accusation.
    career_page_checked       INTEGER NOT NULL,
    scored_at                 TEXT DEFAULT (datetime('now')),
    -- The project's central safety rule, enforced by the database rather than
    -- by everyone remembering it: an unassessed listing cannot carry a score,
    -- and an assessed one cannot be missing one.
    CHECK ((career_page_checked = 0 AND ghost_score IS NULL)
        OR (career_page_checked = 1 AND ghost_score IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_scores_listing ON ghost_scores(listing_id);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT,
    collector_id   TEXT NOT NULL,
    target_url     TEXT,
    status         TEXT,       -- 'success'|'partial'|'failed'|'healed'
    rows_returned  INTEGER,
    -- Rows the collector returned that were NOT at the company they were filed
    -- under. The board search is full text, so a listing merely mentioning the
    -- company comes back looking like one at it. A high count here means the
    -- keyword is ambiguous, and that is worth seeing.
    rows_rejected  INTEGER DEFAULT 0,
    started_at     TEXT,
    completed_at   TEXT,
    heal_event     TEXT,
    raw_path       TEXT        -- data/snapshots/<run>.json
);
