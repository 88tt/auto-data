-- Run once against the Neon DB before the first pipeline run.
-- Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS activities_centres (
    name        TEXT PRIMARY KEY,
    address     TEXT,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    fullname    TEXT,
    url         TEXT,
    created_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS activities_series (
    name          TEXT PRIMARY KEY,
    min_age       DOUBLE PRECISION,
    max_age       DOUBLE PRECISION,
    description   TEXT,
    session_count INTEGER,
    created_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS activities_courses (
    name          TEXT PRIMARY KEY,
    crs_name      TEXT,
    description   TEXT,
    min_age       DOUBLE PRECISION,
    max_age       DOUBLE PRECISION,
    num_sessions  INTEGER,
    created_at    TIMESTAMPTZ,
    series_id     TEXT REFERENCES activities_series(name),
    crs_name_desc TEXT
);

CREATE TABLE IF NOT EXISTS activities_coursenames (
    name          TEXT PRIMARY KEY,
    series_id     TEXT REFERENCES activities_series(name),
    crs_name_desc TEXT,
    min_age       DOUBLE PRECISION,
    max_age       DOUBLE PRECISION,
    num_sessions  INTEGER
);

CREATE TABLE IF NOT EXISTS activities_sessions (
    barcode       TEXT PRIMARY KEY,
    course_id     TEXT REFERENCES activities_courses(name),
    centre_id     TEXT REFERENCES activities_centres(name),
    day_of_week   TEXT,
    start_time    TIME,
    end_time      TIME,
    start_date    DATE,
    end_date      DATE,
    min_age       DOUBLE PRECISION,
    max_age       DOUBLE PRECISION,
    session_url   TEXT,
    availability  VARCHAR(16) NOT NULL DEFAULT 'open'
                  CHECK (availability IN ('full', 'open', 'unknown')),
    has_enroll_now BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ
);

-- Time-series log of availability status per session per scrape run.
-- One row inserted per session per pipeline execution; never updated.
CREATE TABLE IF NOT EXISTS session_availability_log (
    id             BIGSERIAL PRIMARY KEY,
    barcode        TEXT NOT NULL REFERENCES activities_sessions(barcode),
    scraped_at     TIMESTAMPTZ NOT NULL,
    availability   VARCHAR(16) NOT NULL CHECK (availability IN ('full', 'open', 'unknown')),
    has_enroll_now BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_sal_barcode_scraped
    ON session_availability_log (barcode, scraped_at);

CREATE INDEX IF NOT EXISTS idx_sal_scraped_at
    ON session_availability_log (scraped_at);

CREATE TABLE IF NOT EXISTS activities_businesses (
    business      TEXT PRIMARY KEY,
    url           TEXT,
    bus_info      TEXT,
    programs      TEXT,
    age           TEXT,
    relevant_info TEXT
);

CREATE TABLE IF NOT EXISTS activities_businesslocations (
    id          SERIAL PRIMARY KEY,
    loc_name    TEXT,
    address     TEXT,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    business_id TEXT REFERENCES activities_businesses(business),
    activity    TEXT
);
