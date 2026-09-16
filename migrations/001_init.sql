-- PostgreSQL / Supabase schema. Applied automatically by src.storage.migrate().
-- Kept here as well so the schema can be reviewed and applied by hand.

CREATE TABLE IF NOT EXISTS subjects (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT DEFAULT '',
    active      BOOLEAN DEFAULT TRUE,
    priority    INTEGER DEFAULT 100,
    meta        TEXT DEFAULT '{}',
    min_interval_minutes INTEGER DEFAULT 0,
    first_seen  TIMESTAMPTZ,
    last_seen   TIMESTAMPTZ,
    last_read   TIMESTAMPTZ
);

ALTER TABLE subjects ADD COLUMN IF NOT EXISTS min_interval_minutes INTEGER DEFAULT 0;
ALTER TABLE subjects ADD COLUMN IF NOT EXISTS last_read TIMESTAMPTZ;

-- one row per (subject, watched key): what we saw last time
CREATE TABLE IF NOT EXISTS watch_state (
    subject_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    label      TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    url        TEXT DEFAULT '',
    first_seen TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (subject_id, key)
);

-- append-only log of every detected change
CREATE TABLE IF NOT EXISTS watch_events (
    id          BIGSERIAL PRIMARY KEY,
    subject_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    old_value   TEXT DEFAULT '',
    new_value   TEXT NOT NULL,
    label       TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    detected_at TIMESTAMPTZ NOT NULL,
    notified    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS watch_events_idx ON watch_events (subject_id, detected_at DESC);

-- history of numeric watch values (commit rates etc), for the baseline engine
CREATE TABLE IF NOT EXISTS subject_observations (
    id         BIGSERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    value      DOUBLE PRECISION NOT NULL,
    local_day  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subject_obs_idx ON subject_observations (subject_id, key, ts DESC);

-- what we notified about, for de-duplication
CREATE TABLE IF NOT EXISTS alerts (
    id                BIGSERIAL PRIMARY KEY,
    venue_id          TEXT NOT NULL,
    kind              TEXT NOT NULL,
    load_score        DOUBLE PRECISION NOT NULL DEFAULT 0,
    baseline_score    DOUBLE PRECISION NOT NULL DEFAULT 0,
    deviation_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
    metric_type       TEXT NOT NULL,
    sent_at           TIMESTAMPTZ NOT NULL,
    message_hash      TEXT NOT NULL DEFAULT '',
    delivered         BOOLEAN NOT NULL DEFAULT TRUE,
    direction         TEXT NOT NULL DEFAULT 'high'
);
CREATE INDEX IF NOT EXISTS alerts_venue_idx ON alerts (venue_id, kind, sent_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    kind        TEXT NOT NULL,
    stats       TEXT NOT NULL DEFAULT '{}'
);
