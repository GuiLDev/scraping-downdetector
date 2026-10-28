"""Acesso ao SQLite (stdlib). SQL sempre parametrizado; horarios em UTC ISO-8601."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from bankmon.collectors.base import CollectionResult, InstitutionRef, Observation
from bankmon.config import Config
from bankmon.status import InstitutionInfo, RunView
from bankmon.timeutil import from_iso, to_iso

SCHEMA_VERSION = 1
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


def init_db(conn: sqlite3.Connection) -> None:
    """Cria o schema se o banco for novo; falha se for de versao mais nova."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
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
