"""Acesso ao SQLite (stdlib). SQL sempre parametrizado; horarios em UTC ISO-8601."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from bankmon.collectors.base import (
    CollectionResult,
    InstitutionRef,
    Observation,
    ServiceRecord,
    SourceInfo,
)
from bankmon.config import Config
from bankmon.status import InstitutionInfo, RunView
from bankmon.timeutil import from_iso, to_iso

SCHEMA_VERSION = 6
V2_MARKER = "-- @v2"
V3_MARKER = "-- @v3"
V4_MARKER = "-- @v4"
V5_MARKER = "-- @v5"
V6_MARKER = "-- @v6"
COLLECTOR_STATES = ("ativo", "pausado", "verificando")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class DatabaseError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    """Abre o banco em autocommit (transacoes explicitas), WAL e FKs ligadas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; qualquer excecao faz ROLLBACK e e repropagada."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _section_statements(start: str, end: str | None) -> list[str]:
    """DDL aditivo de schema.sql entre dois marcadores `-- @vN` (sem comentarios nem PRAGMA)."""
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    part = text.split(start, 1)[1]
    if end is not None:
        part = part.split(end, 1)[0]
    lines = part.splitlines()[1:]  # descarta o resto da linha do marcador
    lines = [ln for ln in lines if not ln.lstrip().startswith("--")]
    statements = [s.strip() for s in chr(10).join(lines).split(";") if s.strip()]
    return [s for s in statements if not s.upper().startswith("PRAGMA")]


def _v2_statements() -> list[str]:
    return _section_statements(V2_MARKER, V3_MARKER)


def _v3_statements() -> list[str]:
    return _section_statements(V3_MARKER, V4_MARKER)


def _v4_statements() -> list[str]:
    return _section_statements(V4_MARKER, V5_MARKER)


def _v5_statements() -> list[str]:
    return _section_statements(V5_MARKER, V6_MARKER)


def _v6_statements() -> list[str]:
    return _section_statements(V6_MARKER, None)


def init_db(conn: sqlite3.Connection) -> None:
    """Cria o schema se o banco for novo; migra v1..v5 -> v6 (so DDL aditivo; a unica tabela
    recriada, run_sources, tem os dados copiados) e falha se a versao for maior que a suportada."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    elif version in (1, 2, 3, 4, 5):
        with transaction(conn):
            statements = (
                (_v2_statements() if version == 1 else [])
                + (_v3_statements() if version <= 2 else [])
                + (_v4_statements() if version <= 3 else [])
                + (_v5_statements() if version <= 4 else [])
                + _v6_statements()
            )
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise DatabaseError(
            f"versão do schema {version} incompatível (esperada {SCHEMA_VERSION})"
        )


def sync_config(conn: sqlite3.Connection, cfg: Config) -> None:
    """Upsert de categorias e instituicoes; o que sumiu do TOML vira enabled=0."""
    with transaction(conn):
        for cat in cfg.categories:
            conn.execute(
                "INSERT INTO categories (id, name, sort_order) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "sort_order = excluded.sort_order",
                (cat.id, cat.name, cat.order),
            )
        for inst in cfg.institutions:
            conn.execute(
                "INSERT INTO institutions (id, name, category_id, dd_label, enabled) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "category_id = excluded.category_id, dd_label = excluded.dd_label, "
                "enabled = excluded.enabled",
                (inst.id, inst.name, inst.category, inst.dd_label, int(inst.enabled)),
            )
        ids = [i.id for i in cfg.institutions]
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE institutions SET enabled = 0 WHERE id NOT IN ({marks})",  # noqa: S608
            ids,
        )


def open_db(cfg: Config) -> sqlite3.Connection:
    """Abre, migra (se novo) e sincroniza com o TOML."""
    conn = connect(cfg.db_path)
    try:
        init_db(conn)
        sync_config(conn, cfg)
    except BaseException:
        conn.close()
        raise
    return conn


def list_institution_infos(conn: sqlite3.Connection) -> list[InstitutionInfo]:
    rows = conn.execute(
        "SELECT i.id, i.name, i.category_id, c.name AS category_name, "
        "c.sort_order AS category_order "
        "FROM institutions i JOIN categories c ON c.id = i.category_id "
        "WHERE i.enabled = 1 ORDER BY c.sort_order, i.name"
    ).fetchall()
    return [
        InstitutionInfo(r["id"], r["name"], r["category_id"], r["category_name"], r["category_order"])
        for r in rows
    ]


def list_institution_refs(conn: sqlite3.Connection) -> list[InstitutionRef]:
    rows = conn.execute(
        "SELECT id, name, dd_label FROM institutions WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [InstitutionRef(r["id"], r["name"], r["dd_label"]) for r in rows]


def _run_view(conn: sqlite3.Connection, row: sqlite3.Row) -> RunView:
    levels = {
        r["institution_id"]: r["level"]
        for r in conn.execute(
            "SELECT institution_id, level FROM observations WHERE run_id = ?", (row["id"],)
        )
    }
    return RunView(
        run_id=row["id"],
        finished_at=from_iso(row["finished_at"]),
        source_status=row["source_status"],
        is_demo=bool(row["is_demo"]),
        levels=levels,
        error_detail=row["error_detail"],
    )


def latest_run(conn: sqlite3.Connection) -> RunView | None:
    """Execucao mais recente, de qualquer status."""
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return _run_view(conn, row) if row else None


def latest_ok_run(conn: sqlite3.Connection) -> RunView | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE source_status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _run_view(conn, row) if row else None


def runs_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[RunView]:
    """Execucoes com finished_at em [start, end], em ordem cronologica."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE finished_at >= ? AND finished_at <= ? "
        "ORDER BY finished_at, id",
        (to_iso(start), to_iso(end)),
    ).fetchall()
    return [_run_view(conn, r) for r in rows]


def has_runs_since(conn: sqlite3.Connection, since: datetime) -> bool:
    row = conn.execute("SELECT 1 FROM runs WHERE finished_at >= ? LIMIT 1", (to_iso(since),))
    return row.fetchone() is not None


def insert_run(conn: sqlite3.Connection, result: CollectionResult, trigger: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, "
        "source_status, error_detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            to_iso(result.started_at),
            to_iso(result.finished_at),
            result.collector,
            int(result.is_demo),
            trigger,
            result.source_status,
            result.error_detail,
        ),
    )
    return int(cur.lastrowid)


def insert_observations(
    conn: sqlite3.Connection, run_id: int, observations: Sequence[Observation]
) -> None:
    conn.executemany(
        "INSERT INTO observations (run_id, institution_id, level, detail) VALUES (?, ?, ?, ?)",
        [(run_id, o.institution_id, o.level, o.detail) for o in observations],
    )


def insert_run_source(
    conn: sqlite3.Connection, run_id: int, source: SourceInfo, imported_at: datetime
) -> None:
    conn.execute(
        "INSERT INTO run_sources (run_id, kind, file_name, sha256, size_bytes, captured_at, "
        "imported_at, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            source.kind,
            source.file_name,
            source.sha256,
            source.size_bytes,
            to_iso(source.captured_at),
            to_iso(imported_at),
            source.url,
        ),
    )


def insert_run_services(
    conn: sqlite3.Connection, run_id: int, services: Sequence[ServiceRecord]
) -> None:
    conn.executemany(
        "INSERT INTO run_services (run_id, position, service, status, status_original, color, "
        "consistent, issue, institution_id, svg_viewbox, svg_width, svg_height, svg_path_d) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id, s.position, s.service, s.status, s.status_original, s.color,
                int(s.consistent), s.issue, s.institution_id, s.svg_viewbox, s.svg_width,
                s.svg_height, s.svg_path_d,
            )
            for s in services
        ],
    )


def insert_transitions(
    conn: sqlite3.Connection,
    run_id: int,
    transitions: Sequence[tuple[str, str, str]],
    changed_at: datetime,
) -> None:
    """`transitions`: (institution_id, from_state, to_state)."""
    conn.executemany(
        "INSERT INTO state_transitions (institution_id, run_id, from_state, to_state, changed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(iid, run_id, frm, to, to_iso(changed_at)) for iid, frm, to in transitions],
    )


# --- Consultas de leitura do dashboard ----------------------------------------


@dataclass(frozen=True)
class TransitionRow:
    institution_id: str
    from_state: str
    to_state: str
    changed_at: datetime
    run_id: int


@dataclass(frozen=True)
class FailedRun:
    run_id: int
    finished_at: datetime
    collector: str
    source_status: str
    is_demo: bool
    error_detail: str | None


def open_readonly(path: Path) -> sqlite3.Connection:
    """Conexao SOMENTE LEITURA (mode=ro + query_only) para o dashboard.

    Levanta `sqlite3.OperationalError` se o arquivo nao existir (nao cria nada).
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_before(conn: sqlite3.Connection, moment: datetime) -> RunView | None:
    """Ultima execucao (qualquer status) com finished_at estritamente antes de `moment`."""
    row = conn.execute(
        "SELECT * FROM runs WHERE finished_at < ? ORDER BY finished_at DESC, id DESC LIMIT 1",
        (to_iso(moment),),
    ).fetchone()
    return _run_view(conn, row) if row else None


def _transition_row(r: sqlite3.Row) -> TransitionRow:
    return TransitionRow(
        r["institution_id"], r["from_state"], r["to_state"], from_iso(r["changed_at"]), r["run_id"]
    )


def last_transition_by_institution(conn: sqlite3.Connection) -> dict[str, TransitionRow]:
    """Transicao mais recente de cada instituicao (base do "desde HH:MM")."""
    rows = conn.execute(
        "SELECT t.* FROM state_transitions t WHERE t.id = ("
        "SELECT id FROM state_transitions WHERE institution_id = t.institution_id "
        "ORDER BY changed_at DESC, id DESC LIMIT 1)"
    ).fetchall()
    return {r["institution_id"]: _transition_row(r) for r in rows}


def transitions_between(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    institution_id: str | None = None,
    limit: int = 500,
) -> list[TransitionRow]:
    """Transicoes com changed_at em [start, end], da mais recente para a mais antiga."""
    sql = "SELECT * FROM state_transitions WHERE changed_at >= ? AND changed_at <= ?"
    params: list[object] = [to_iso(start), to_iso(end)]
    if institution_id is not None:
        sql += " AND institution_id = ?"
        params.append(institution_id)
    sql += " ORDER BY changed_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_transition_row(r) for r in conn.execute(sql, params)]


def failed_runs_between(
    conn: sqlite3.Connection, start: datetime, end: datetime, limit: int = 200
) -> list[FailedRun]:
    """Execucoes com source_status != ok em [start, end], da mais recente para a mais antiga."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE source_status != 'ok' AND finished_at >= ? AND finished_at <= ? "
        "ORDER BY finished_at DESC, id DESC LIMIT ?",
        (to_iso(start), to_iso(end), limit),
    ).fetchall()
    return [
        FailedRun(
            r["id"],
            from_iso(r["finished_at"]),
            r["collector"],
            r["source_status"],
            bool(r["is_demo"]),
            r["error_detail"],
        )
        for r in rows
    ]


def run_source(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """Origem (arquivo) da execucao, ou None se ela nao veio de importacao."""
    return conn.execute("SELECT * FROM run_sources WHERE run_id = ?", (run_id,)).fetchone()


def run_services(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Servicos gravados da execucao, na ordem da pagina."""
    return conn.execute(
        "SELECT * FROM run_services WHERE run_id = ? ORDER BY position", (run_id,)
    ).fetchall()


def source_sha256_exists(conn: sqlite3.Connection, sha256: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM run_sources WHERE sha256 = ? LIMIT 1", (sha256,)
    ).fetchone() is not None


# --- Estado da coleta pelo navegador (v3, por origem desde a v6) -------------------------------


@dataclass(frozen=True)
class CollectorState:
    estado: str  # um de COLLECTOR_STATES
    motivo: str | None
    desde: datetime | None  # None so quando nunca houve mudanca (estado padrao "ativo")
    falhas_seguidas: int = 0  # leituras seguidas sem sucesso da origem (v6)


# --- Estado da coleta por origem (v6) ---------------------------------------------------------------


def get_origin_state(conn: sqlite3.Connection, origin: str) -> CollectorState:
    """Estado da origem: a linha propria; senao a linha global legada (congelada na v6, fallback
    conservador); senao `ativo` com 0 falhas. Funciona em conexao somente leitura."""
    row = conn.execute(
        "SELECT estado, motivo, desde, falhas_seguidas FROM collector_origin_state WHERE origin = ?",
        (origin,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT estado, motivo, desde, falhas_seguidas FROM collector_state WHERE id = 1"
        ).fetchone()
    if row is None:
        return CollectorState("ativo", None, None)
    if row["estado"] not in COLLECTOR_STATES:
        raise ValueError("estado da coleta desconhecido")
    return CollectorState(
        row["estado"], row["motivo"], from_iso(row["desde"]), falhas_seguidas=row["falhas_seguidas"]
    )


def external_suspension(conn: sqlite3.Connection, reasons: Sequence[str]) -> tuple[str, datetime] | None:
    """`(motivo, desde)` da suspensao mais antiga gravada em QUALQUER origem (pausada com um dos
    `reasons`), ou None. Funciona em conexao somente leitura."""
    marks = ",".join("?" * len(reasons))
    row = conn.execute(
        "SELECT motivo, desde FROM collector_origin_state WHERE estado = 'pausado' "  # noqa: S608
        f"AND motivo IN ({marks}) ORDER BY desde, origin LIMIT 1",
        tuple(reasons),
    ).fetchone()
    return (row["motivo"], from_iso(row["desde"])) if row else None


def write_origin_state(
    conn: sqlite3.Connection, origin: str, estado: str, motivo: str | None, falhas: int, now: datetime
) -> None:
    """Upsert do estado da origem SEM abrir transacao: o chamador ja esta numa (ex.: `on_record` do
    runner, junto com a execucao). `desde` so muda quando o estado muda (o motivo sempre e gravado)."""
    if not conn.in_transaction:
        raise DatabaseError("write_origin_state exige uma transação aberta")
    if estado not in COLLECTOR_STATES:
        raise ValueError(f"estado da coleta inválido: {estado!r}")
    current = get_origin_state(conn, origin)
    since = current.desde if current.estado == estado and current.desde is not None else now
    conn.execute(
        "INSERT INTO collector_origin_state (origin, estado, motivo, desde, atualizado_em, "
        "falhas_seguidas) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(origin) DO UPDATE SET "
        "estado = excluded.estado, motivo = excluded.motivo, desde = excluded.desde, "
        "atualizado_em = excluded.atualizado_em, falhas_seguidas = excluded.falhas_seguidas",
        (origin, estado, motivo, to_iso(since), to_iso(now), falhas),
    )


def set_origin_state(
    conn: sqlite3.Connection, origin: str, estado: str, motivo: str | None, now: datetime, falhas: int = 0
) -> None:
    """`write_origin_state` numa transacao propria (retomada, testes)."""
    with transaction(conn):
        write_origin_state(conn, origin, estado, motivo, falhas, now)


# --- Reservas da vaga de leitura (v4) --------------------------------------------------------------


def last_reservation_at(conn: sqlite3.Connection, origin: str) -> datetime | None:
    """Ultima reserva da origem (consumida ou nao): a recarga acontece depois dela."""
    row = conn.execute(
        "SELECT MAX(reserved_at) AS at FROM collector_reservations WHERE origin = ?", (origin,)
    ).fetchone()
    return from_iso(row["at"]) if row["at"] else None


def has_open_reservation(conn: sqlite3.Connection, origin: str, now: datetime) -> bool:
    """Ha reserva nao consumida e ainda nao expirada para a origem?"""
    return conn.execute(
        "SELECT 1 FROM collector_reservations WHERE origin = ? AND consumed_at IS NULL "
        "AND expires_at > ? LIMIT 1",
        (origin, to_iso(now)),
    ).fetchone() is not None


def insert_reservation(
    conn: sqlite3.Connection, reservation_id: str, origin: str, reserved_at: datetime, expires_at: datetime
) -> None:
    conn.execute(
        "INSERT INTO collector_reservations (id, origin, reserved_at, expires_at) VALUES (?, ?, ?, ?)",
        (reservation_id, origin, to_iso(reserved_at), to_iso(expires_at)),
    )


def prune_reservations(conn: sqlite3.Connection, origin: str, keep_id: str, now: datetime) -> None:
    """Apaga as reservas consumidas ou expiradas da origem, exceto `keep_id` (a mais recente)."""
    conn.execute(
        "DELETE FROM collector_reservations WHERE origin = ? AND id <> ? "
        "AND (consumed_at IS NOT NULL OR expires_at <= ?)",
        (origin, keep_id, to_iso(now)),
    )


def is_reservation_valid(conn: sqlite3.Connection, reservation_id: str, origin: str, now: datetime) -> bool:
    return conn.execute(
        "SELECT 1 FROM collector_reservations WHERE id = ? AND origin = ? AND consumed_at IS NULL "
        "AND expires_at > ?",
        (reservation_id, origin, to_iso(now)),
    ).fetchone() is not None


def consume_reservation(
    conn: sqlite3.Connection, reservation_id: str, origin: str, now: datetime
) -> bool:
    """Marca a reserva como consumida; False se nao for valida (outra origem, usada ou expirada)."""
    cur = conn.execute(
        "UPDATE collector_reservations SET consumed_at = ? WHERE id = ? AND origin = ? "
        "AND consumed_at IS NULL AND expires_at > ?",
        (to_iso(now), reservation_id, origin, to_iso(now)),
    )
    return cur.rowcount == 1
