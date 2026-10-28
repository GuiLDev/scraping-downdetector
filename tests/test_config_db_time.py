"""Config (validacoes extras), schema/db e timeutil."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bankmon import db
from bankmon.config import ConfigError, load_config
from bankmon.collectors.base import CollectionResult
from bankmon.timeutil import ensure_utc, fmt_local, from_iso, to_iso, to_local
from conftest import T0, TOML, db_rows


def _bad(write_config, text, message):
    write_config(text)
    with pytest.raises(ConfigError, match=message):
        load_config()


# --- 9. Config -----------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "::", "example.com", "", "localhost", "127.0.0.1/8"])
def test_non_loopback_hosts_rejected(write_config, host):
    _bad(write_config, TOML.format(collector="demo").replace('"127.0.0.1"', f'"{host}"'), "server.host")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_hosts_accepted(write_config, host):
    write_config(TOML.format(collector="demo").replace('"127.0.0.1"', f'"{host}"'))
    assert load_config().host == host


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("interval_minutes = 60", "interval_minutes = 0", "interval_minutes"),
        ("interval_minutes = 60", "interval_minutes = -5", "interval_minutes"),
        ("interval_minutes = 60", "interval_minutes = true", "interval_minutes"),
        ("interval_minutes = 60", 'interval_minutes = "60"', "interval_minutes"),
        ("stale_after_minutes = 90", "stale_after_minutes = 59", "stale_after_minutes"),
        ("port = 8765", "port = 0", "port"),
        ("port = 8765", "port = 70000", "port"),
        ("seed = 42", 'seed = "x"', "seed"),
        ('id = "inst-1"', 'id = "Inst 1"', "id inválido"),
        ('id = "inst-1"', 'id = "../x"', "id inválido"),
        ('category = "a"', "category = 3", "category"),
        ('dd_label = "Inst 1"', "", "dd_label"),
        ('active = "demo"', "", "active"),
    ],
)
def test_invalid_values_raise_config_error(write_config, old, new, message):
    _bad(write_config, TOML.format(collector="demo").replace(old, new, 1), message)


def test_stale_equal_to_interval_is_accepted(write_config):
    write_config(TOML.format(collector="demo").replace("stale_after_minutes = 90", "stale_after_minutes = 60"))
    assert load_config().stale_after_minutes == 60


def test_missing_sections_are_reported(write_config):
    text = TOML.format(collector="demo")
    _bad(write_config, text.replace("[schedule]", "[agenda]"), "schedule")
    _bad(write_config, text[: text.index("[[categories]]")] + text[text.index("[[institutions]]"):], "categories")
    _bad(write_config, text[: text.index("[[institutions]]")], "institutions")


def test_enabled_defaults_to_true_and_must_be_boolean(write_config):
    write_config()
    assert all(i.enabled for i in load_config().institutions)
    _bad(
        write_config,
        TOML.format(collector="demo").replace('dd_label = "Inst 2"', 'dd_label = "Inst 2"\nenabled = 1'),
        "enabled",
    )


def test_data_dir_and_config_come_from_environment(write_config, tmp_path):
    write_config()
    cfg = load_config()
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.lock_path.parent == cfg.data_dir
    assert cfg.log_dir == cfg.data_dir / "logs" and cfg.backup_dir == cfg.data_dir / "backups"


def test_explicit_arguments_override_environment(write_config, tmp_path):
    path = write_config()
    other = tmp_path / "outro"
    cfg = load_config(path, other)
    assert cfg.data_dir == other


@pytest.mark.parametrize("bad", ["categories = 'x'", "categories = [1]", "institutions = [1]"])
def test_malformed_tables_should_raise_config_error(write_config, bad):
    text = TOML.format(collector="demo")
    head = text[: text.index("[[categories]]")]
    cats = text[text.index("[[categories]]"): text.index("[[institutions]]")]
    insts = text[text.index("[[institutions]]"):]
    if bad.startswith("categories"):
        text = bad + "\n" + head + insts
    else:
        text = bad + "\n" + head + cats
    write_config(text)
    with pytest.raises(ConfigError):
        load_config()


# --- Schema e db -------------------------------------------------------------------------------


@pytest.fixture
def conn(cfg):
    c = db.open_db(cfg)
    yield c
    c.close()


def test_schema_tables_columns_and_version(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"categories", "institutions", "runs", "observations", "state_transitions"} <= tables
    cols = lambda t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
    assert cols("categories") == ["id", "name", "sort_order"]
    assert cols("institutions") == ["id", "name", "category_id", "dd_label", "enabled"]
    assert cols("runs") == [
        "id", "started_at", "finished_at", "collector", "is_demo", "trigger",
        "source_status", "error_detail",
    ]
    assert cols("observations") == ["run_id", "institution_id", "level", "detail"]
    assert cols("state_transitions") == [
        "id", "institution_id", "run_id", "from_state", "to_state", "changed_at",
    ]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 1


def test_wal_and_foreign_keys_enabled(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_transition_index_exists(conn):
    idx = {r["name"]: r for r in conn.execute("PRAGMA index_list(state_transitions)")}
    assert "idx_transitions_inst_changed" in idx
    cols = [r["name"] for r in conn.execute("PRAGMA index_info(idx_transitions_inst_changed)")]
    assert cols == ["institution_id", "changed_at"]


def test_init_db_is_idempotent_and_keeps_data(cfg):
    db.open_db(cfg).close()
    conn = db.open_db(cfg)
    conn.execute("INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)"
                 " VALUES ('a','b','c',0,'cli','ok')")
    conn.close()
    conn = db.open_db(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM institutions").fetchone()[0] == 2
    finally:
        conn.close()


def test_incompatible_schema_version_is_rejected(cfg):
    db.open_db(cfg).close()
    raw = sqlite3.connect(cfg.db_path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(db.DatabaseError, match="incompatível"):
        db.open_db(cfg)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)"
        " VALUES ('a','b','c',0,'cli','bogus')",
        "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)"
        " VALUES ('a','b','c',0,'manual','ok')",
        "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)"
        " VALUES ('a','b','c',2,'cli','ok')",
        "INSERT INTO observations (run_id, institution_id, level) VALUES (999,'inst-1','no_alert')",
        "INSERT INTO institutions (id, name, category_id, dd_label, enabled) VALUES ('z','Z','nao-existe','Z',1)",
        "INSERT INTO institutions (id, name, category_id, dd_label, enabled) VALUES ('z','Z','a','Z',5)",
    ],
)
def test_schema_constraints_reject_invalid_rows(conn, sql):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_observation_primary_key_and_level_check(conn):
    conn.execute("INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status)"
                 " VALUES ('a','b','c',0,'cli','ok')")
    conn.execute("INSERT INTO observations (run_id, institution_id, level) VALUES (1,'inst-1','critical')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO observations (run_id, institution_id, level) VALUES (1,'inst-1','attention')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO observations (run_id, institution_id, level) VALUES (1,'inst-2','verde')")


def test_runs_stored_as_utc_iso_and_queries_ordered(cfg):
    conn = db.open_db(cfg)
    try:
        sao_paulo = timezone(timedelta(hours=-3))
        for k, status in enumerate(["ok", "error", "ok"]):
            at = datetime(2026, 10, 28, 9 + k, 0, tzinfo=sao_paulo)  # 12h, 13h, 14h UTC
            res = CollectionResult("c", False, at, at, status)
            db.insert_run(conn, res, "cli")
        assert [r[0] for r in conn.execute("SELECT finished_at FROM runs ORDER BY id")] == [
            "2026-10-28T12:00:00Z", "2026-10-28T13:00:00Z", "2026-10-28T14:00:00Z",
        ]
        assert db.latest_run(conn).source_status == "ok"
        assert db.latest_run(conn).finished_at == datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
        assert db.latest_ok_run(conn).run_id == 3
        between = db.runs_between(conn, T0, T0 + timedelta(hours=1))
        assert [r.run_id for r in between] == [1, 2]  # limites inclusivos
        assert db.has_runs_since(conn, T0 + timedelta(hours=2))
        assert not db.has_runs_since(conn, T0 + timedelta(hours=2, seconds=1))
    finally:
        conn.close()


def test_latest_run_on_empty_db_is_none(conn):
    assert db.latest_run(conn) is None and db.latest_ok_run(conn) is None


def test_latest_run_tie_on_finished_at_prefers_highest_id(cfg):
    conn = db.open_db(cfg)
    try:
        db.insert_run(conn, CollectionResult("c", False, T0, T0, "ok"), "cli")
        db.insert_run(conn, CollectionResult("c", False, T0, T0, "error"), "cli")
        assert db.latest_run(conn).run_id == 2
    finally:
        conn.close()


def test_naive_datetime_is_rejected_before_writing(cfg):
    conn = db.open_db(cfg)
    try:
        naive = datetime(2026, 10, 28, 12, 0)
        with pytest.raises(ValueError, match="fuso"):
            db.insert_run(conn, CollectionResult("c", False, naive, naive, "ok"), "cli")
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_sql_injection_in_ids_is_stored_literally(cfg):
    conn = db.open_db(cfg)
    try:
        db.insert_run(conn, CollectionResult("c'; DROP TABLE runs;--", False, T0, T0, "ok"), "cli")
        assert conn.execute("SELECT collector FROM runs").fetchone()[0] == "c'; DROP TABLE runs;--"
    finally:
        conn.close()
    assert db_rows(cfg, "SELECT COUNT(*) FROM runs") == [(1,)]


# --- timeutil ----------------------------------------------------------------------------------


def test_to_iso_from_iso_roundtrip_and_truncates_microseconds():
    dt = datetime(2026, 10, 28, 12, 0, 5, 999999, tzinfo=timezone.utc)
    assert to_iso(dt) == "2026-10-28T12:00:05Z"
    assert from_iso("2026-10-28T12:00:05Z") == dt.replace(microsecond=0)


def test_to_iso_converts_other_timezones_to_utc():
    dt = datetime(2026, 10, 28, 9, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert to_iso(dt) == "2026-10-28T12:00:00Z"


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 10, 28, 12, 0))
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 10, 28, 12, 0))


def test_display_in_sao_paulo():
    assert fmt_local(T0) == "28/10/2026 09:00"
    assert to_local(T0).utcoffset() == timedelta(hours=-3)


@pytest.mark.parametrize("stale,ok", [(60, True), (240, True), (241, False), (59, False)])
def test_stale_after_bounds_with_interval_60(write_config, stale, ok):
    text = TOML.format(collector="demo").replace("stale_after_minutes = 90", f"stale_after_minutes = {stale}")
    write_config(text)
    if ok:
        assert load_config().stale_after_minutes == stale
    else:
        with pytest.raises(ConfigError, match="stale_after_minutes"):
            load_config()


def test_stale_after_capped_at_one_day(write_config):
    text = TOML.format(collector="demo").replace("interval_minutes = 60", "interval_minutes = 600")
    write_config(text.replace("stale_after_minutes = 90", "stale_after_minutes = 1441"))
    with pytest.raises(ConfigError, match="1440"):
        load_config()
    write_config(text.replace("stale_after_minutes = 90", "stale_after_minutes = 1440"))
    assert load_config().stale_after_minutes == 1440
