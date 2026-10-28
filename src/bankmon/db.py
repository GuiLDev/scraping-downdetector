"""Acesso ao SQLite (stdlib). SQL sempre parametrizado; horarios em UTC ISO-8601."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
