-- Horarios em UTC, ISO-8601 (ex.: 2026-10-28T12:00:00Z).
CREATE TABLE categories (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);

CREATE TABLE institutions (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES categories(id),
    dd_label    TEXT NOT NULL,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    collector     TEXT NOT NULL,
    is_demo       INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    trigger       TEXT NOT NULL CHECK (trigger IN ('scheduled', 'cli')),
    source_status TEXT NOT NULL
        CHECK (source_status IN ('ok', 'inaccessible', 'error', 'not_authorized')),
    error_detail  TEXT
);
CREATE INDEX idx_runs_finished_at ON runs (finished_at);

CREATE TABLE observations (
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    level          TEXT NOT NULL
        CHECK (level IN ('no_alert', 'attention', 'critical', 'not_listed')),
    detail         TEXT,
    PRIMARY KEY (run_id, institution_id)
);

CREATE TABLE state_transitions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    from_state     TEXT NOT NULL
        CHECK (from_state IN ('critical', 'attention', 'no_alert', 'no_data', 'not_monitored')),
    to_state       TEXT NOT NULL
        CHECK (to_state IN ('critical', 'attention', 'no_alert', 'no_data', 'not_monitored')),
    changed_at     TEXT NOT NULL
);
CREATE INDEX idx_transitions_inst_changed ON state_transitions (institution_id, changed_at);

PRAGMA user_version = 1;
