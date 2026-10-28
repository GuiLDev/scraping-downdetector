import os
import time
from datetime import timedelta

import pytest

from bankmon import db, runner
from bankmon.collectors.base import CollectionResult, Observation
from bankmon.collectors.demo import DemoCollector
from bankmon.collectors.downdetector import PENDING_MESSAGE, DowndetectorCollector
from conftest import T0, assert_lock_free


class FakeCollector:
    name = "fake"
    is_demo = True  # cfg de teste e demo

    def __init__(self, status="ok", levels=None, at=T0, boom=False):
        self.status, self.levels, self.at, self.boom = status, levels or {}, at, boom

    def collect(self, institutions):
        if self.boom:
            raise RuntimeError("falhou")
        return CollectionResult(
            "fake", True, self.at, self.at, self.status,
            tuple(Observation(i, lvl, "x") for i, lvl in self.levels.items()),
        )


def rows(cfg, sql, params=()):
    conn = db.connect(cfg.db_path)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_first_run_records_transitions(cfg):
    fake = FakeCollector(levels={"inst-1": "critical", "inst-2": "no_alert"})
    out = runner.run_collection(cfg, "cli", now=T0, collector=fake)
    assert out.transitions == 2
    got = rows(cfg, "SELECT institution_id, from_state, to_state FROM state_transitions ORDER BY 1")
    assert got == [("inst-1", "no_data", "critical"), ("inst-2", "no_data", "no_alert")]


def test_no_transition_when_state_unchanged_and_expiry_applied(cfg):
    levels = {"inst-1": "no_alert", "inst-2": "no_alert"}
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels=levels, at=T0))
    out = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels=levels, at=T0 + timedelta(minutes=60))
    )
    assert out.transitions == 0
    # 91 min depois da ultima: o estado "antes" expirou para no_data, entao ha transicao
    out = runner.run_collection(
        cfg, "cli", collector=FakeCollector(levels=levels, at=T0 + timedelta(minutes=151))
    )
    assert out.transitions == 2
    assert rows(cfg, "SELECT DISTINCT from_state, to_state FROM state_transitions WHERE run_id=?",
                (out.run_id,)) == [("no_data", "no_alert")]


def test_failed_run_moves_everything_to_no_data(cfg):
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "attention"}))
    out = runner.run_collection(
        cfg, "cli", collector=FakeCollector(status="error", at=T0 + timedelta(minutes=5))
    )
    assert out.transitions == 1
    assert rows(cfg, "SELECT to_state FROM state_transitions WHERE run_id=?", (out.run_id,)) == [
        ("no_data",)
    ]


def test_collector_exception_becomes_error_run(cfg):
    out = runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector(boom=True))
    assert out.result.source_status == "error"
    assert "RuntimeError" in out.result.error_detail
    assert rows(cfg, "SELECT source_status FROM runs") == [("error",)]


def test_invalid_observation_becomes_error_run(cfg):
    out = runner.run_collection(
        cfg, "cli", now=T0, collector=FakeCollector(levels={"fantasma": "critical"})
    )
    assert out.result.source_status == "error"
    assert rows(cfg, "SELECT COUNT(*) FROM observations") == [(0,)]


def test_stub_downdetector_is_not_authorized_and_never_green(write_config):
    from bankmon.config import load_config

    write_config(collector="downdetector")
    cfg = load_config()
    out = runner.run_collection(cfg, "scheduled", now=T0)
    assert out.result.source_status == "not_authorized"
    assert out.result.error_detail == PENDING_MESSAGE
    assert out.result.observations == () and not out.result.is_demo
    assert rows(cfg, "SELECT COUNT(*) FROM state_transitions") == [(0,)]
    assert cfg.db_path.name == "bankmon.db"
    assert DowndetectorCollector.is_demo is False


def test_atomic_write_rolls_back_everything(cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("falha no meio da gravacao")

    monkeypatch.setattr(db, "insert_transitions", boom)
    fake = FakeCollector(levels={"inst-1": "critical", "inst-2": "no_alert"})
    with pytest.raises(RuntimeError):
        runner.run_collection(cfg, "cli", now=T0, collector=fake)
    assert rows(cfg, "SELECT COUNT(*) FROM runs") == [(0,)]
    assert rows(cfg, "SELECT COUNT(*) FROM observations") == [(0,)]
    assert_lock_free(cfg.lock_path)  # lock liberado mesmo com falha


def test_lock_busy_and_released(cfg):
    with runner.file_lock(cfg.lock_path):
        with pytest.raises(runner.LockBusy):
            runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())
    runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())


def test_leftover_lock_file_does_not_block(cfg):
    cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.lock_path.write_text("999")
    old = time.time() - 31 * 60
    os.utime(cfg.lock_path, (old, old))
    out = runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())
    assert out.result.source_status == "ok"
    assert cfg.lock_path.exists()  # nunca e apagado


def test_second_acquisition_fails_while_first_active_then_works(tmp_path):
    lock = tmp_path / "x.lock"
    with runner.file_lock(lock):
        with pytest.raises(runner.LockBusy):
            with runner.file_lock(lock):
                pass
    with runner.file_lock(lock):
        pass


def test_held_lock_is_respected(cfg):
    with runner.file_lock(cfg.lock_path):
        with pytest.raises(runner.LockBusy):
            runner.run_collection(cfg, "cli", now=T0, collector=FakeCollector())
    assert cfg.lock_path.exists()


def test_sync_disables_removed_institution(cfg, write_config):
    from bankmon.config import load_config
    from conftest import TOML

    conn = db.open_db(cfg)
    conn.close()
    text = TOML.format(collector="demo")
    text = text[: text.index('[[institutions]]\nid = "inst-2"')]
    write_config(text)
    conn = db.open_db(load_config())
    conn.close()
    assert rows(cfg, "SELECT id, enabled FROM institutions ORDER BY id") == [
        ("inst-1", 1),
        ("inst-2", 0),
    ]


def test_demo_collector_is_deterministic(cfg):
    conn = db.open_db(cfg)
    refs = db.list_institution_refs(conn)
    conn.close()
    a = DemoCollector(seed=1, clock=lambda: T0).collect(refs)
    b = DemoCollector(seed=1, clock=lambda: T0).collect(refs)
    assert a == b and a.is_demo
    assert all("sintético" in (o.detail or "") for o in a.observations)


def test_seed_demo_generates_history(cfg):
    count = runner.seed_demo(cfg, 2, now=T0)
    assert count == 48
    assert rows(cfg, "SELECT COUNT(*) FROM runs") == [(48,)]
    assert rows(cfg, "SELECT COUNT(*) FROM state_transitions")[0][0] > 0
    with pytest.raises(ValueError, match="já tem execuções"):
        runner.seed_demo(cfg, 2, now=T0)


def test_seed_demo_refused_for_real_collector(write_config):
    from bankmon.config import load_config

    write_config(collector="downdetector")
    with pytest.raises(ValueError, match="demo"):
        runner.seed_demo(load_config(), 1)
