-- PostgreSQL / Supabase schema. Applied automatically by src.storage.migrate().
-- Kept here as well so the schema can be reviewed and applied by hand.

CREATE TABLE IF NOT EXISTS venues (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    address          TEXT DEFAULT '',
    latitude         DOUBLE PRECISION NOT NULL,
    longitude        DOUBLE PRECISION NOT NULL,
    distance_meters  DOUBLE PRECISION DEFAULT 0,
    category         TEXT DEFAULT '',
    website          TEXT DEFAULT '',
    phone            TEXT DEFAULT '',
    delivery         BOOLEAN DEFAULT FALSE,
    takeaway         BOOLEAN DEFAULT FALSE,
    sources          TEXT DEFAULT '[]',
    cuisine          TEXT DEFAULT '',
    opening_hours    TEXT DEFAULT '',
    business_status  TEXT DEFAULT 'OPERATIONAL',
    source_ids       TEXT DEFAULT '{}',
    active           BOOLEAN DEFAULT TRUE,
    first_seen       TIMESTAMPTZ,
    last_seen        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS venues_active_idx ON venues (active, distance_meters);

CREATE TABLE IF NOT EXISTS observations (
    id             BIGSERIAL PRIMARY KEY,
    venue_id       TEXT NOT NULL REFERENCES venues (id) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL,
    source         TEXT NOT NULL,
    metric_type    TEXT NOT NULL,
    metric_value   DOUBLE PRECISION NOT NULL,
    raw_value      TEXT,
    load_score     DOUBLE PRECISION NOT NULL,
    domain         TEXT NOT NULL DEFAULT 'unknown',
    confidence     DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    signal_quality TEXT NOT NULL DEFAULT 'medium',
    local_weekday  SMALLINT NOT NULL,
    local_minutes  SMALLINT NOT NULL
);

-- the exact access path used by the baseline query
CREATE INDEX IF NOT EXISTS observations_baseline_idx
    ON observations (venue_id, metric_type, local_weekday, local_minutes, ts DESC);
CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations (ts);

CREATE TABLE IF NOT EXISTS baselines (
    venue_id      TEXT NOT NULL,
    metric_type   TEXT NOT NULL,
    weekday       SMALLINT NOT NULL,
    minutes       SMALLINT NOT NULL,
    sample_count  INTEGER NOT NULL,
    median        DOUBLE PRECISION NOT NULL,
    mad           DOUBLE PRECISION NOT NULL,
    p90           DOUBLE PRECISION NOT NULL,
    mean          DOUBLE PRECISION NOT NULL,
    status        TEXT NOT NULL,
    computed_at   TIMESTAMPTZ,
    PRIMARY KEY (venue_id, metric_type, weekday, minutes)
);

CREATE TABLE IF NOT EXISTS alerts (
    id                BIGSERIAL PRIMARY KEY,
    venue_id          TEXT NOT NULL,
    kind              TEXT NOT NULL,
    load_score        DOUBLE PRECISION NOT NULL,
    baseline_score    DOUBLE PRECISION NOT NULL,
    deviation_percent DOUBLE PRECISION NOT NULL,
    metric_type       TEXT NOT NULL,
    sent_at           TIMESTAMPTZ NOT NULL,
    message_hash      TEXT NOT NULL DEFAULT '',
    delivered         BOOLEAN NOT NULL DEFAULT TRUE,
    -- 'high' = unusually busy, 'low' = unusually quiet
    direction         TEXT NOT NULL DEFAULT 'high'
);

CREATE INDEX IF NOT EXISTS alerts_venue_idx ON alerts (venue_id, kind, sent_at DESC);

-- one row per venue: "is it currently in an alerting state?"
CREATE TABLE IF NOT EXISTS alert_state (
    venue_id       TEXT PRIMARY KEY,
    metric_type    TEXT NOT NULL DEFAULT '',
    active         BOOLEAN NOT NULL DEFAULT FALSE,
    last_alert_at  TIMESTAMPTZ,
    last_score     DOUBLE PRECISION DEFAULT 0,
    last_deviation DOUBLE PRECISION DEFAULT 0,
    peak_score     DOUBLE PRECISION DEFAULT 0,
    direction      TEXT NOT NULL DEFAULT 'high',
    updated_at     TIMESTAMPTZ
);

ALTER TABLE alerts      ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'high';
ALTER TABLE alert_state ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'high';

CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    kind        TEXT NOT NULL,
    stats       TEXT NOT NULL DEFAULT '{}'
);
