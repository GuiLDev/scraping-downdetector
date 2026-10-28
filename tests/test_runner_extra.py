"""Runner: atomicidade, transicoes, lockfile, separacao demo x real, sincronizacao do TOML."""

import os
import time
from datetime import timedelta

import pytest

from bankmon import db, runner
from bankmon.collectors.base import CollectionResult, Observation
from bankmon.config import load_config
from conftest import T0, TOML, FakeCollector, assert_lock_free, db_rows

MIN = timedelta(minutes=1)


def counts(cfg):
    return {
        t: db_rows(cfg, f"SELECT COUNT(*) FROM {t}")[0][0]
        for t in ("runs", "observations", "state_transitions")
    }


# --- 6. Gravacao atomica -------------------------------------------------------


def test_atomic_failure_after_partial_transitions_leaves_nothing(cfg, monkeypatch):
    real = db.insert_transitions

    def partial_then_boom(conn, run_id, transitions, changed_at):
        real(conn, run_id, transitions[:1], changed_at)  # grava UMA transicao e quebra
        raise RuntimeError("queda no meio")

    monkeypatch.setattr(db, "insert_transitions", partial_then_boom)
    fake = FakeCollector(levels={"inst-1": "critical", "inst-2": "attention"})
    with pytest.raises(RuntimeError, match="queda"):
        runner.run_collection(cfg, "cli", now=T0, collector=fake)
    assert counts(cfg) == {"runs": 0, "observations": 0, "state_transitions": 0}


def test_atomic_failure_while_inserting_observations(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falha em observations")

    monkeypatch.setattr(db, "insert_observations", boom)
    with pytest.raises(RuntimeError):
        runner.run_collection(
            cfg, "cli", now=T0, collector=FakeCollector(levels={"inst-1": "critical"})
        )
    assert counts(cfg) == {"runs": 0, "observations": 0, "state_transitions": 0}
    assert_lock_free(cfg.lock_path)


def test_after_failed_write_next_run_works_and_ids_are_clean(cfg, monkeypatch):
    levels = {"inst-1": "critical", "inst-2": "no_alert"}
    with monkeypatch.context() as m:
        m.setattr(db, "insert_transitions", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        with pytest.raises(OSError):
            runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector(levels=levels))
    out = runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector(levels=levels))
    assert counts(cfg) == {"runs": 1, "observations": 2, "state_transitions": 2}
    # o estado "antes" da primeira execucao valida continua sendo no_data
    got = db_rows(cfg, "SELECT from_state FROM state_transitions WHERE run_id=?", (out.run_id,))
    assert {g[0] for g in got} == {"no_data"}


def test_real_constraint_violation_rolls_back_whole_run(cfg):
    """Violacao real de CHECK no banco (trigger invalido) nao deixa run parcial."""
    conn = db.open_db(cfg)
    try:
        res = CollectionResult("fake", False, T0, T0, "ok", (Observation("inst-1", "no_alert"),))
        with pytest.raises(Exception):
            runner.record_result(conn, cfg, res, "xyz")  # ValueError antes de gravar
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        with pytest.raises(Exception):
            with db.transaction(conn):
                db.insert_run(conn, res, "cli")
                conn.execute(
                    "INSERT INTO observations (run_id, institution_id, level) VALUES (1,'inst-1','bogus')"
                )
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        conn.close()


# --- 6. Excecao do coletor / resultado invalido ----------------------------------


def test_collector_exception_is_recorded_with_details(cfg):
    out = runner.run_collection(
        cfg, "scheduled", now=T0, collector=FakeCollector(boom=True, is_demo=True)
    )
    assert out.result.source_status == "error"
    row = db_rows(cfg, "SELECT source_status, error_detail, is_demo, trigger, collector FROM runs")
    assert row == [("error", "RuntimeError: falhou", 1, "scheduled", "fake")]


@pytest.mark.parametrize(
    "result_kwargs,fragment",
    [
        ({"source_status": "bogus"}, "source_status inválido"),
        ({"observations": (Observation("inst-1", "azul"),)}, "nível inválido"),
        ({"observations": (Observation("zzz", "no_alert"),)}, "desconhecida"),
        (
            {"observations": (Observation("inst-1", "no_alert"), Observation("inst-1", "critical"))},
            "duplicada",
        ),
    ],
)
def test_invalid_collector_result_becomes_error_run(cfg, result_kwargs, fragment):
    class Bad(FakeCollector):
        def collect(self, institutions):
            base = dict(
                collector="fake", is_demo=True, started_at=T0, finished_at=T0,
                source_status="ok", observations=(),
            )
            base.update(result_kwargs)
            return CollectionResult(**base)

    out = runner.run_collection(cfg, "cli", collector=Bad())
    assert out.result.source_status == "error"
    assert fragment in out.result.error_detail
    assert counts(cfg)["observations"] == 0
    assert db_rows(cfg, "SELECT source_status FROM runs") == [("error",)]


def test_observation_for_disabled_institution_is_rejected(cfg, write_config):
    runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())
    text = TOML.format(collector="demo")
    write_config(text[: text.index('[[institutions]]\nid = "inst-2"')])
    new_cfg = load_config()
    out = runner.run_collection(
        new_cfg, "cli", now=T0 + MIN, collector=FakeCollector(levels={"inst-2": "critical"})
    )
    assert out.result.source_status == "error"


def test_invalid_trigger_is_rejected_and_nothing_written(cfg):
    with pytest.raises(ValueError, match="trigger"):
        runner.run_collection(cfg, "manual", now=T0, collector=FakeCollector())
    assert counts(cfg)["runs"] == 0


# --- 6. Transicoes ----------------------------------------------------------------


def transitions(cfg, run_id=None):
    sql = "SELECT institution_id, from_state, to_state FROM state_transitions"
    params = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    return db_rows(cfg, sql + " ORDER BY id", params)


def test_no_data_to_attention_after_expiry(cfg):
    levels = {"inst-1": "attention", "inst-2": "no_alert"}
    first = runner.run_collection(cfg, "cli", collector=FakeCollector(levels=levels, at=T0))
    assert ("inst-1", "no_data", "attention") in transitions(cfg, first.run_id)
    same = runner.run_collection(cfg, "cli", collector=FakeCollector(levels=levels, at=T0 + 60 * MIN))
    assert transitions(cfg, same.run_id) == []  # estado igual e dentro do prazo
    late = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels=levels, at=T0 + 60 * MIN + 91 * MIN)
    )
    got = transitions(cfg, late.run_id)
    assert ("inst-1", "no_data", "attention") in got
    assert ("inst-2", "no_data", "no_alert") in got
    assert db_rows(
        cfg, "SELECT changed_at FROM state_transitions WHERE run_id=?", (late.run_id,)
    )[0] == ("2026-10-28T14:31:00Z",)


def test_exactly_at_deadline_still_no_transition(cfg):
    levels = {"inst-1": "attention", "inst-2": "attention"}
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels=levels, at=T0))
    out = runner.run_collection(cfg, "cli", collector=FakeCollector(levels=levels, at=T0 + 90 * MIN))
    assert out.transitions == 0


def test_consecutive_failures_record_a_single_transition(cfg):
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "critical"}, at=T0))
    a = runner.run_collection(cfg, "cli", collector=FakeCollector(status="error", at=T0 + 5 * MIN))
    b = runner.run_collection(
        cfg, "cli", collector=FakeCollector(status="inaccessible", at=T0 + 10 * MIN)
    )
    assert transitions(cfg, a.run_id) == [("inst-1", "critical", "no_data")]
    assert b.transitions == 0


def test_recovery_after_failure_and_level_changes(cfg):
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "attention"}, at=T0))
    runner.run_collection(cfg, "cli", collector=FakeCollector(status="error", at=T0 + 5 * MIN))
    rec = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels={"inst-1": "critical"}, at=T0 + 10 * MIN)
    )
    assert transitions(cfg, rec.run_id) == [("inst-1", "no_data", "critical")]
    esc = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels={"inst-1": "no_alert"}, at=T0 + 20 * MIN)
    )
    assert transitions(cfg, esc.run_id) == [("inst-1", "critical", "no_alert")]


def test_not_listed_transition_and_missing_observation(cfg):
    out = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels={"inst-1": "not_listed"}, at=T0)
    )
    # not_listed (ausente da pagina) e no_data: sem transicao, como inst-2 sem observacao
    assert transitions(cfg, out.run_id) == []


def test_transition_rows_reference_run_and_use_utc_iso(cfg):
    out = runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "critical"}, at=T0))
    rows = db_rows(cfg, "SELECT run_id, changed_at FROM state_transitions")
    assert rows == [(out.run_id, "2026-10-28T12:00:00Z")]


def test_failed_first_run_has_no_transitions(cfg):
    out = runner.run_collection(cfg, "cli", collector=FakeCollector(status="not_authorized", at=T0))
    assert out.transitions == 0 and counts(cfg)["state_transitions"] == 0


# --- 6. Lockfile ------------------------------------------------------------------


def test_lock_file_is_kept_after_release_and_reacquirable(tmp_path):
    lock = tmp_path / "sub" / "x.lock"
    with runner.file_lock(lock):
        with pytest.raises(runner.LockBusy):
            with runner.file_lock(lock):
                pass
        assert lock.exists()  # a tentativa falha nao afeta o dono
    assert lock.exists()  # nao apagado ao sair (evita corrida de remocao)
    with runner.file_lock(lock):
        pass


def test_lock_released_when_body_raises(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(KeyError):
        with runner.file_lock(lock):
            raise KeyError("x")
    with runner.file_lock(lock):
        pass


def test_lock_busy_writes_no_run_and_lock_is_not_stolen(cfg):
    with runner.file_lock(cfg.lock_path):
        with pytest.raises(runner.LockBusy):
            runner.seed_demo(cfg, 1, now=T0)
        with pytest.raises(runner.LockBusy):
            runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())
        assert cfg.lock_path.exists()
    assert not cfg.db_path.exists() or counts(cfg)["runs"] == 0


# --- 8. Separacao demo x real -------------------------------------------------------


def test_demo_and_real_use_separate_databases(write_config):
    write_config(collector="demo")
    demo_cfg = load_config()
    write_config(collector="downdetector")
    real_cfg = load_config()
    assert demo_cfg.data_dir == real_cfg.data_dir
    assert demo_cfg.db_path.name == "bankmon-demo.db"
    assert real_cfg.db_path.name == "bankmon.db"

    runner.run_collection(demo_cfg, "cli", now=T0)  # demo
    runner.run_collection(real_cfg, "cli", now=T0 + MIN)  # stub downdetector

    assert db_rows(demo_cfg, "SELECT collector, is_demo FROM runs") == [("demo", 1)]
    assert db_rows(real_cfg, "SELECT collector, is_demo, source_status FROM runs") == [
        ("downdetector", 0, "not_authorized")
    ]


def test_every_demo_run_is_flagged_is_demo(cfg):
    runner.seed_demo(cfg, 1, now=T0)
    runner.run_collection(cfg, "cli", now=T0 + 2 * MIN)
    assert db_rows(cfg, "SELECT DISTINCT is_demo FROM runs") == [(1,)]
    assert db_rows(cfg, "SELECT DISTINCT collector FROM runs") == [("demo",)]


def test_seed_demo_refused_when_active_collector_is_not_demo_and_creates_nothing(write_config):
    write_config(collector="downdetector")
    real_cfg = load_config()
    with pytest.raises(ValueError, match="demo"):
        runner.seed_demo(real_cfg, 1, now=T0)
    assert not real_cfg.db_path.exists()
    assert not (real_cfg.data_dir / "bankmon-demo.db").exists()


# --- seed-demo ------------------------------------------------------------------------


def test_seed_demo_one_day_is_hourly_and_chronological(cfg):
    assert runner.seed_demo(cfg, 1, now=T0) == 24
    times = [r[0] for r in db_rows(cfg, "SELECT finished_at FROM runs ORDER BY id")]
    assert times == sorted(times) and len(set(times)) == 24
    assert times[0] == "2026-10-27T12:00:00Z" and times[-1] == "2026-10-28T11:00:00Z"
    changed = [r[0] for r in db_rows(cfg, "SELECT changed_at FROM state_transitions ORDER BY id")]
    assert changed == sorted(changed)


def test_seed_demo_invalid_days_and_overlap(cfg):
    for days in (0, -3):
        with pytest.raises(ValueError, match="maior que zero"):
            runner.seed_demo(cfg, days, now=T0)
    runner.seed_demo(cfg, 1, now=T0)
    with pytest.raises(ValueError, match="já tem execuções"):
        runner.seed_demo(cfg, 1, now=T0)
    assert counts(cfg)["runs"] == 24  # a recusa nao gravou nada


def test_seed_demo_allowed_when_existing_runs_are_older_than_period(cfg):
    runner.run_collection(cfg, "cli", now=T0 - timedelta(days=3))
    assert runner.seed_demo(cfg, 1, now=T0) == 24
    assert counts(cfg)["runs"] == 25


def test_seed_demo_is_reproducible_across_databases(write_config, tmp_path, monkeypatch):
    write_config()
    a = load_config()
    runner.seed_demo(a, 1, now=T0)
    snap_a = db_rows(a, "SELECT finished_at, source_status FROM runs ORDER BY id")
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "data2"))
    b = load_config()
    runner.seed_demo(b, 1, now=T0)
    assert snap_a == db_rows(b, "SELECT finished_at, source_status FROM runs ORDER BY id")


# --- 12. Sincronizacao do TOML ----------------------------------------------------------


def test_removed_institution_keeps_history_and_is_excluded_from_new_collections(cfg, write_config):
    runner.run_collection(
        cfg,
        "cli",
        collector=FakeCollector(levels={"inst-1": "critical", "inst-2": "attention"}, at=T0),
    )
    text = TOML.format(collector="demo")
    write_config(text[: text.index('[[institutions]]\nid = "inst-2"')])
    new_cfg = load_config()
    conn = db.open_db(new_cfg)
    try:
        assert [r.id for r in db.list_institution_refs(conn)] == ["inst-1"]
        assert [i.id for i in db.list_institution_infos(conn)] == ["inst-1"]
    finally:
        conn.close()
    assert db_rows(new_cfg, "SELECT id, enabled FROM institutions ORDER BY id") == [
        ("inst-1", 1),
        ("inst-2", 0),
    ]
    # historico preservado
    assert db_rows(new_cfg, "SELECT COUNT(*) FROM observations WHERE institution_id='inst-2'") == [(1,)]
    assert db_rows(new_cfg, "SELECT COUNT(*) FROM state_transitions WHERE institution_id='inst-2'") == [(1,)]
    # e uma nova coleta nao gera transicao para a instituicao removida
    out = runner.run_collection(
        new_cfg, "cli", collector=FakeCollector(levels={"inst-1": "no_alert"}, at=T0 + 10 * MIN)
    )
    assert all(t[0] != "inst-2" for t in transitions(new_cfg, out.run_id))


def test_reinstated_institution_is_enabled_again(cfg, write_config):
    text = TOML.format(collector="demo")
    write_config(text[: text.index('[[institutions]]\nid = "inst-2"')])
    db.open_db(load_config()).close()
    write_config()  # volta ao TOML completo
    db.open_db(load_config()).close()
    assert db_rows(cfg, "SELECT id, enabled FROM institutions ORDER BY id") == [
        ("inst-1", 1),
        ("inst-2", 1),
    ]


def test_enabled_false_in_toml_disables_institution(cfg, write_config):
    text = TOML.format(collector="demo").replace(
        'dd_label = "Inst 2"', 'dd_label = "Inst 2"\nenabled = false'
    )
    write_config(text)
    db.open_db(load_config()).close()
    assert db_rows(cfg, "SELECT enabled FROM institutions WHERE id='inst-2'") == [(0,)]


def test_sync_updates_names_categories_and_is_idempotent(cfg, write_config):
    db.open_db(cfg).close()
    write_config(
        TOML.format(collector="demo")
        .replace('name = "Inst 1"', 'name = "Renomeada"')
        .replace('name = "Cat B"', 'name = "Cat B2"\n#')
        .replace('order = 2', 'order = 7')
    )
    new_cfg = load_config()
    for _ in range(2):
        db.open_db(new_cfg).close()
    assert db_rows(new_cfg, "SELECT name FROM institutions WHERE id='inst-1'") == [("Renomeada",)]
    assert db_rows(new_cfg, "SELECT name, sort_order FROM categories WHERE id='b'") == [("Cat B2", 7)]
    assert db_rows(new_cfg, "SELECT COUNT(*) FROM institutions") == [(2,)]


# --- Problemas conhecidos (xfail strict: viram falha quando corrigidos) ----------------------


def test_collector_returning_naive_datetimes_should_become_error_run(cfg):
    from datetime import datetime

    class Naive(FakeCollector):
        def collect(self, institutions):
            naive = datetime(2026, 10, 28, 12, 0)
            return CollectionResult("fake", True, naive, naive, "ok")

    out = runner.run_collection(cfg, "cli", collector=Naive())
    assert out.result.source_status == "error"


def test_future_finished_at_becomes_error_run(cfg):
    out = runner.run_collection(
        cfg, "cli", now=T0, collector=FakeCollector(levels={"inst-1": "critical"}, at=T0 + 3 * MIN)
    )
    assert out.result.source_status == "error" and "futuro" in out.result.error_detail
    assert out.result.finished_at == T0
    ok = runner.run_collection(
        cfg, "cli", now=T0, collector=FakeCollector(levels={"inst-1": "critical"}, at=T0 + MIN)
    )
    assert ok.result.source_status == "ok"


def test_started_after_finished_becomes_error_run(cfg):
    class Backwards(FakeCollector):
        def collect(self, institutions):
            return CollectionResult("fake", True, T0 + MIN, T0, "ok")

    out = runner.run_collection(cfg, "cli", now=T0 + 2 * MIN, collector=Backwards())
    assert out.result.source_status == "error" and "started_at" in out.result.error_detail


def test_is_demo_mismatch_becomes_error_run(cfg):
    out = runner.run_collection(
        cfg, "cli", now=T0, collector=FakeCollector(levels={"inst-1": "critical"}, is_demo=False)
    )
    assert out.result.source_status == "error" and "is_demo" in out.result.error_detail
    assert out.result.is_demo is True
    assert db_rows(cfg, "SELECT is_demo, source_status FROM runs") == [(1, "error")]


def test_latest_run_orders_by_id_not_finished_at(cfg):
    from bankmon import db as dbm

    runner.run_collection(cfg, "cli", now=T0 + 10 * MIN, collector=FakeCollector(at=T0 + 10 * MIN))
    runner.run_collection(cfg, "cli", now=T0 + 10 * MIN, collector=FakeCollector(at=T0, status="error"))
    conn = dbm.connect(cfg.db_path)
    try:
        assert dbm.latest_run(conn).source_status == "error"
        assert dbm.latest_ok_run(conn).source_status == "ok"
    finally:
        conn.close()


# --- SEC-009: validacao de tipos --------------------------------------------------


def _run_bad(cfg, **overrides):
    class Bad(FakeCollector):
        def collect(self, institutions):
            base = dict(
                collector="fake", is_demo=True, started_at=T0, finished_at=T0,
                source_status="ok", observations=(), error_detail=None,
            )
            base.update(overrides)
            return CollectionResult(**base)

    return runner.run_collection(cfg, "cli", now=T0, collector=Bad())


@pytest.mark.parametrize(
    "overrides",
    [
        {"collector": 123},
        {"collector": "outro"},
        {"is_demo": 1},
        {"is_demo": "True"},
        {"started_at": "2026-10-28"},
        {"finished_at": 5},
        {"source_status": 7},
        {"error_detail": 42},
        {"observations": None},
        {"observations": "abc"},
        {"observations": (object(),)},
        {"observations": (Observation(1, "no_alert"),)},
        {"observations": (Observation("inst-1", None),)},
        {"observations": (Observation("inst-1", "no_alert", 99),)},
        {"observations": ({"institution_id": "inst-1"},)},
    ],
)
def test_wrong_types_become_error_run_never_traceback(cfg, overrides):
    out = _run_bad(cfg, **overrides)
    assert out.result.source_status == "error"
    assert out.result.collector == "demo" or out.result.collector == "fake"
    assert out.result.is_demo is True
    assert db_rows(cfg, "SELECT source_status, is_demo FROM runs") == [("error", 1)]
    assert db_rows(cfg, "SELECT COUNT(*) FROM observations") == [(0,)]


def test_validation_exception_becomes_error_run(cfg):
    class Weird:
        def __bool__(self):
            raise RuntimeError("boom")

    out = _run_bad(cfg, observations=(Observation("inst-1", "no_alert", Weird()),))
    assert out.result.source_status == "error"
    assert db_rows(cfg, "SELECT source_status FROM runs") == [("error",)]


def test_detail_and_error_detail_are_sanitized_and_truncated(cfg):
    out = _run_bad(
        cfg,
        error_detail="x" * 1000,
        observations=(Observation("inst-1", "no_alert", "ok token=SEGREDO1 " + "y" * 1000),),
    )
    assert out.result.source_status == "ok"
    assert len(out.result.error_detail) <= 300
    detail = db_rows(cfg, "SELECT detail FROM observations")[0][0]
    assert len(detail) <= 300 and "SEGREDO" not in detail


def test_collector_name_must_match_active_collector(cfg):
    # sem injecao do nome esperado (record_result direto), vale cfg.collector ("demo")
    from bankmon import db as dbm

    conn = dbm.open_db(cfg)
    try:
        res = CollectionResult("fake", True, T0, T0, "ok")
        out = runner.record_result(conn, cfg, res, "cli", lambda: T0)
        assert out.result.source_status == "error" and out.result.collector == "demo"
    finally:
        conn.close()


@pytest.mark.parametrize("returned", [None, {"collector": "fake"}, "ok"])
def test_collector_returning_non_result_is_recorded_as_error(cfg, returned):
    class Broken(FakeCollector):
        def collect(self, institutions):
            return returned

    out = runner.run_collection(cfg, "cli", now=T0, collector=Broken())
    assert out.result.source_status == "error"
    assert "CollectionResult" in out.result.error_detail
    assert db_rows(cfg, "SELECT source_status FROM runs") == [("error",)]
