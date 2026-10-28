from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bankmon.config import load_config

TOML = """
[collector]
active = "{collector}"

[schedule]
interval_minutes = 60
stale_after_minutes = 90

[server]
host = "127.0.0.1"
port = 8765

[demo]
seed = 42

[[categories]]
id = "a"
name = "Cat A"
order = 1

[[categories]]
id = "b"
name = "Cat B"
order = 2

[[institutions]]
id = "inst-1"
name = "Inst 1"
category = "a"
dd_label = "Inst 1"

[[institutions]]
id = "inst-2"
name = "Inst 2"
category = "b"
dd_label = "Inst 2"
"""

T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Escreve um TOML temporario e aponta BANKMON_CONFIG/BANKMON_DATA_DIR para tmp_path."""

    def _write(text: str | None = None, collector: str = "demo"):
        path = tmp_path / "cfg.toml"
        path.write_text(text if text is not None else TOML.format(collector=collector), "utf-8")
        monkeypatch.setenv("BANKMON_CONFIG", str(path))
        monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "data"))
        return path

    return _write


@pytest.fixture
def cfg(write_config):
    write_config()
    return load_config()


# --- Fixtures e helpers compartilhados (ampliacao E1.0/E1.1) ------------------

import logging
import sqlite3

from datetime import timedelta

from bankmon.collectors.base import CollectionResult, InstitutionRef, Observation


@pytest.fixture(autouse=True)
def _restore_bankmon_logging():
    """`cli.setup_logging` mexe no logger global "bankmon" (propagate=False, handlers de
    arquivo). Sem restaurar, testes seguintes que usam `caplog` deixam de enxergar os logs e o
    arquivo de log fica aberto (problema no Windows ao limpar tmp_path)."""
    yield
    logger = logging.getLogger("bankmon")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _fixed_runner_clock(monkeypatch):
    """Relogio do runner fixo logo apos T0: os coletores falsos usam T0 + offsets e o runner
    rejeita `finished_at` no futuro. Testes que precisam de outro relogio sobrescrevem."""
    from bankmon import runner

    monkeypatch.setattr(runner, "utc_now", lambda: T0 + timedelta(days=2))


@pytest.fixture
def project_config(monkeypatch, tmp_path):
    """Config real do projeto (15 instituicoes) com dados isolados em tmp_path."""
    from bankmon.config import DEFAULT_CONFIG_PATH

    monkeypatch.setenv("BANKMON_CONFIG", str(DEFAULT_CONFIG_PATH))
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def db_rows(cfg, sql, params=()):
    """Consulta somente-leitura no banco do `cfg` (que SEMPRE aponta para tmp_path)."""
    assert "tmp" in str(cfg.data_dir).lower() or "pytest" in str(cfg.data_dir).lower()
    conn = sqlite3.connect(cfg.db_path)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def refs_for(cfg):
    return [InstitutionRef(i.id, i.name, i.dd_label) for i in cfg.institutions if i.enabled]


def find_ok_moment(cfg, start=T0, hours=500):
    """Primeiro instante (hora cheia) em que o coletor demo NAO simula falha."""
    from bankmon.collectors.demo import DemoCollector

    refs = refs_for(cfg)
    for h in range(hours):
        moment = start + timedelta(hours=h)
        res = DemoCollector(seed=cfg.demo_seed, clock=lambda m=moment: m).collect(refs)
        if res.source_status == "ok":
            return moment
    raise AssertionError("nenhuma hora sem falha encontrada")


class FakeCollector:
    """Coletor injetavel: devolve `levels` com o `status` informado, ou levanta excecao."""

    name = "fake"
    is_demo = False

    def __init__(self, status="ok", levels=None, at=T0, boom=False, is_demo=True):
        self.status, self.levels, self.at, self.boom = status, levels or {}, at, boom
        self.is_demo = is_demo

    def collect(self, institutions):
        if self.boom:
            raise RuntimeError("falhou")
        return CollectionResult(
            "fake", self.is_demo, self.at, self.at, self.status,
            tuple(Observation(i, lvl, "x") for i, lvl in self.levels.items()),
        )


def assert_lock_free(lock_path):
    """O lock do SO foi liberado: da para adquiri-lo de novo (o arquivo permanece)."""
    from bankmon import runner

    with runner.file_lock(lock_path):
        pass
