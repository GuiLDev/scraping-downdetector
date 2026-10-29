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

-- @v2 (tudo abaixo e aditivo: um banco v1 migra executando so esta parte)
-- Origem de uma execucao importada de arquivo (HTML exportado manualmente).
CREATE TABLE run_sources (
    run_id      INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind = 'html_offline'),
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
    captured_at TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_run_sources_sha256 ON run_sources (sha256);

-- Todos os servicos da pagina importada. Nao guarda o stroke cru, so o enum de cor.
CREATE TABLE run_services (
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL CHECK (position >= 0),
    service         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('normal', 'warning', 'critical', 'unknown')),
    status_original TEXT,
    color           TEXT CHECK (color IS NULL OR color IN ('red', 'yellow', 'blue', 'other')),
    consistent      INTEGER NOT NULL CHECK (consistent IN (0, 1)),
    issue           TEXT,
    institution_id  TEXT REFERENCES institutions(id),
    svg_viewbox     TEXT,
    svg_width       TEXT,
    svg_height      TEXT,
    svg_path_d      TEXT,
    PRIMARY KEY (run_id, position)
);

-- @v3 (aditivo: um banco v2 migra executando so esta parte)
-- Estado da coleta pelo navegador (linha unica; ausente = ativo).
CREATE TABLE collector_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    estado        TEXT NOT NULL CHECK (estado IN ('ativo', 'pausado', 'verificando')),
    motivo        TEXT,
    desde         TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);

-- run_sources passa a aceitar kind = 'browser' (com a URL, sem query string). O SQLite nao altera
-- um CHECK: a tabela e recriada com os dados copiados. Como o navegador pode ler o mesmo HTML
-- duas vezes, so o offline continua unico por SHA-256 (indice parcial).
DROP INDEX idx_run_sources_sha256;
ALTER TABLE run_sources RENAME TO run_sources_v2;
CREATE TABLE run_sources (
    run_id      INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('html_offline', 'browser')),
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
    captured_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    url         TEXT
);
INSERT INTO run_sources (run_id, kind, file_name, sha256, size_bytes, captured_at, imported_at)
    SELECT run_id, kind, file_name, sha256, size_bytes, captured_at, imported_at FROM run_sources_v2;
DROP TABLE run_sources_v2;
CREATE UNIQUE INDEX idx_run_sources_sha256 ON run_sources (sha256) WHERE kind = 'html_offline';

-- @v4 (aditivo: um banco v3 migra executando so esta parte)
-- Reserva da vaga de leitura de uma origem, feita ANTES da recarga da aba. O limite de frequencia
-- vem destas linhas (sobrevive a reinicios); a reserva e consumida junto com a gravacao da execucao.
CREATE TABLE collector_reservations (
    id          TEXT PRIMARY KEY,
    origin      TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX idx_reservations_origin ON collector_reservations (origin, reserved_at);

PRAGMA user_version = 4;
