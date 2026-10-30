"""Coleta pelo navegador, camada 1: coletor, schema v3 e configuracao (TDD)."""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest

from bankmon import db, runner
from bankmon.collectors import registry
from bankmon.collectors.base import InstitutionRef
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.collectors.offline_html import OfflineHtmlCollector
from browser_helpers import PATH_D, URL, blocked_html, page_html
from webutil import INSTITUTIONS, make_cfg

AT = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
REFS = [InstitutionRef(i, n, n) for i, n, _ in INSTITUTIONS]


def collect(html, url=URL):
    return BrowserHtmlCollector(html, url, AT).collect(REFS)


# --- 1. coletor ---------------------------------------------------------------------------


def test_browser_collector_maps_services_and_records_browser_source():
    html = page_html(alfa="enfrenta problemas", beta="possíveis problemas")
    res = collect(html)
    assert (res.collector, res.is_demo, res.source_status) == ("navegador", False, "ok")
    assert {o.institution_id: o.level for o in res.observations} == {
        "alfa": "critical", "beta": "attention", "gama": "no_alert", "delta": "no_alert"}
    assert res.observations[0].detail.startswith("Downdetector (aba do navegador): ")
    alfa = next(s for s in res.services if s.institution_id == "alfa")
    assert (alfa.color, alfa.svg_path_d) == ("red", PATH_D)
    assert res.source.kind == "browser" and res.source.url == URL and res.source.captured_at == AT
    assert res.source.sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert res.source.size_bytes == len(html.encode())


def test_browser_collector_blocked_page_is_inaccessible():
    res = collect(blocked_html())
    assert res.source_status == "inaccessible" and "blocked_by_cloudflare" in res.error_detail
    assert res.observations == () and res.services == ()


def test_browser_collector_incomplete_page_is_error():
    res = collect("<html><body></body></html>")
    assert res.source_status == "error" and "incompleta" in res.error_detail


def test_browser_collector_html_over_5mb_is_error():
    res = collect("<p>" + "x" * (5 * 1024 * 1024 + 1) + "</p>")
    assert res.source_status == "error" and "5 MB" in res.error_detail


def test_offline_collector_behaviour_is_unchanged_by_the_extraction(tmp_path):
    path = tmp_path / "c.html"
    path.write_text(page_html(), "utf-8")
    res = OfflineHtmlCollector(path, AT).collect(REFS)
    assert res.source.kind == "html_offline" and res.source.url is None
    assert res.observations[0].detail.startswith("Downdetector (HTML offline): ")


def test_browser_collector_is_registered_but_not_constructible_from_options():
    assert registry.COLLECTORS["navegador"] is BrowserHtmlCollector
    assert BrowserHtmlCollector.data_kind == "browser" and BrowserHtmlCollector.is_demo is False
    with pytest.raises(ValueError, match="serve --navegador"):
        BrowserHtmlCollector.from_options(seed=0, clock=lambda: AT)


# --- 2. config: banco proprio e nunca coletor ativo ------------------------------------------


def test_browser_db_is_separate_file(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="navegador")
    assert cfg.db_path.name == "bankmon-navegador.db" and not cfg.is_demo and cfg.is_browser


def test_browser_collector_cannot_be_the_active_collector(tmp_path, monkeypatch):
    from bankmon.config import ConfigError

    with pytest.raises(ConfigError, match="navegador"):
        make_cfg(tmp_path, monkeypatch, collector="navegador")


# --- 3. schema v3: estado da coleta e origem "browser" --------------------------------------------


def _v2_db(path):
    """Banco v2 REAL: schema v1 + as tabelas v2 como eram (CHECK so de html_offline)."""
    text = db.SCHEMA_PATH.read_text("utf-8")
    v1, rest = text.split(db.V2_MARKER)
    conn = sqlite3.connect(path)
    conn.executescript(v1)
    conn.executescript("""
        CREATE TABLE run_sources (run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK (kind = 'html_offline'), file_name TEXT NOT NULL,
          sha256 TEXT NOT NULL CHECK (length(sha256) = 64), size_bytes INTEGER NOT NULL,
          captured_at TEXT NOT NULL, imported_at TEXT NOT NULL);
        CREATE UNIQUE INDEX idx_run_sources_sha256 ON run_sources (sha256);
        CREATE TABLE run_services (run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, service TEXT NOT NULL, status TEXT NOT NULL,
          status_original TEXT, color TEXT, consistent INTEGER NOT NULL, issue TEXT,
          institution_id TEXT REFERENCES institutions(id), svg_viewbox TEXT, svg_width TEXT,
          svg_height TEXT, svg_path_d TEXT, PRIMARY KEY (run_id, position));
        INSERT INTO categories VALUES ('c','Cat',1);
        INSERT INTO institutions VALUES ('i1','Inst 1','c','Inst 1',1);
        INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)
          VALUES ('2026-10-28T10:00:00Z','2026-10-28T10:00:00Z','offline_html',0,'cli','ok');
        INSERT INTO run_sources VALUES (1,'html_offline','a.html','%s',10,'2026-10-28T10:00:00Z',
          '2026-10-28T10:00:00Z');
        INSERT INTO run_services VALUES (1,0,'Inst 1','normal',NULL,'blue',1,NULL,'i1',NULL,NULL,NULL,NULL);
        PRAGMA user_version = 2;
    """ % ("ab" * 32))
    conn.close()


def test_v2_database_migrates_to_v5_without_losing_data(tmp_path):
    path = tmp_path / "v2.db"
    _v2_db(path)
    conn = db.connect(path)
    try:
        db.init_db(conn)
        db.init_db(conn)  # idempotente
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        row = conn.execute("SELECT kind, file_name, sha256, url FROM run_sources").fetchone()
        assert tuple(row) == ("html_offline", "a.html", "ab" * 32, None)
        assert conn.execute("SELECT count(*) FROM run_services").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        # agora aceita origem do navegador, inclusive HTML repetido (so o offline e unico)
        for run in (2, 3):
            conn.execute(
                "INSERT INTO runs (id, started_at, finished_at, collector, is_demo, trigger, "
                "source_status) VALUES (?, 'x', 'x', 'navegador', 0, 'scheduled', 'ok')", (run,))
            conn.execute(
                "INSERT INTO run_sources (run_id, kind, file_name, sha256, size_bytes, captured_at, "
                "imported_at, url) VALUES (?, 'browser', 'aba-navegador', ?, 1, 'x', 'x', 'http://h/p')",
                (run, "cd" * 32))
        with pytest.raises(sqlite3.IntegrityError):  # offline continua unico por SHA
            conn.execute(
                "INSERT INTO run_sources (run_id, kind, file_name, sha256, size_bytes, captured_at, "
                "imported_at) VALUES (1, 'html_offline', 'b', ?, 1, 'x', 'x')", ("ab" * 32,))
    finally:
        conn.close()


def test_v1_database_migrates_to_v5(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text("utf-8").split(db.V2_MARKER)[0] + "PRAGMA user_version = 1;")
    conn.close()
    conn = db.connect(path)
    try:
        db.init_db(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM collector_state").fetchone()[0] == 0
    finally:
        conn.close()


def test_collector_state_defaults_to_ativo_and_keeps_desde_while_unchanged(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    try:
        db.init_db(conn)
        assert db.get_collector_state(conn).estado == "ativo"
        db.set_collector_state(conn, "pausado", "bloqueio", AT)
        later = AT.replace(hour=13)
        db.set_collector_state(conn, "pausado", "bloqueio", later)
        state = db.get_collector_state(conn)
        assert (state.estado, state.motivo, state.desde) == ("pausado", "bloqueio", AT)
        db.set_collector_state(conn, "ativo", None, later)
        assert db.get_collector_state(conn).desde == later
        with pytest.raises(ValueError):
            db.set_collector_state(conn, "invalido", None, AT)
    finally:
        conn.close()


def test_runner_persists_browser_source_with_url_and_repeated_html(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="navegador")
    html = page_html()
    for _ in range(2):  # mesmo HTML duas vezes: aceito no navegador
        out = runner.run_collection(cfg, "scheduled", now=AT, collector=BrowserHtmlCollector(html, URL, AT))
        assert out.result.source_status == "ok"
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = conn.execute("SELECT kind, file_name, url FROM run_sources ORDER BY run_id").fetchall()
    finally:
        conn.close()
    assert rows == [("browser", "aba-navegador", URL)] * 2


def test_runner_rejects_browser_source_url_with_query(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="navegador")
    out = runner.run_collection(
        cfg, "scheduled", now=AT, collector=BrowserHtmlCollector(page_html(), URL + "?token=1", AT))
    assert out.result.source_status == "error"


def test_v3_database_migrates_to_v5_adding_reservations_and_keeping_data(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text("utf-8").split(db.V4_MARKER)[0] + "PRAGMA user_version = 3;")
    conn.execute("INSERT INTO categories VALUES ('c', 'Cat', 1)")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    try:
        db.init_db(conn)
        db.init_db(conn)  # idempotente
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM categories").fetchone()[0] == 1
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(collector_reservations)")]
        assert cols == ["id", "origin", "reserved_at", "expires_at", "consumed_at"]
    finally:
        conn.close()


def test_v4_database_migrates_to_v5_keeping_the_failures_after_a_resume(tmp_path):
    """INF-03 (SEC-017): banco v4 em `verificando`: as falhas a partir da retomada continuam contando."""
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text("utf-8").split(db.V5_MARKER)[0] + "PRAGMA user_version = 4;")
    for at, status in (("11:00", "inaccessible"), ("12:00", "error"), ("12:01", "error")):
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status) "
            "VALUES (?, ?, 'navegador', 0, 'scheduled', ?)",
            (f"2026-10-29T{at}:00Z", f"2026-10-29T{at}:00Z", status))
    conn.execute(
        "INSERT INTO collector_state VALUES (1, 'verificando', 'retomada', '2026-10-29T12:00:00Z', "
        "'2026-10-29T12:00:00Z')")
    conn.execute(
        "INSERT INTO collector_reservations VALUES ('r1', 'http://127.0.0.1:8777', "
        "'2026-10-29T12:00:00Z', '2026-10-29T12:02:00Z', NULL)")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    try:
        db.init_db(conn)
        db.init_db(conn)  # idempotente
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        state = db.get_collector_state(conn)
        assert (state.estado, state.motivo, state.apos_execucao) == ("verificando", "retomada", 1)
        assert conn.execute("SELECT id FROM collector_reservations").fetchall()[0][0] == "r1"
        assert db.recent_statuses(conn, 3, state.apos_execucao) == ["error", "error"]
    finally:
        conn.close()
