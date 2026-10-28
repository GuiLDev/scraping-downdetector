import pytest

from bankmon.config import ConfigError, load_config
from conftest import TOML


def test_valid_config_demo_db_name(cfg):
    assert cfg.is_demo
    assert cfg.db_path.name == "bankmon-demo.db"
    assert len(cfg.institutions) == 2


def test_real_collector_db_name(write_config):
    write_config(collector="downdetector")
    cfg = load_config()
    assert not cfg.is_demo
    assert cfg.db_path.name == "bankmon.db"


def test_project_config_is_valid(monkeypatch, tmp_path):
    monkeypatch.delenv("BANKMON_CONFIG", raising=False)
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert cfg.collector == "demo" and len(cfg.institutions) == 15


def _bad(write_config, old, new, message):
    write_config(TOML.format(collector="demo").replace(old, new, 1))
    with pytest.raises(ConfigError, match=message):
        load_config()


def test_unknown_collector(write_config):
    _bad(write_config, 'active = "demo"', 'active = "xyz"', "coletor desconhecido")


def test_unknown_category(write_config):
    _bad(write_config, 'category = "a"', 'category = "zzz"', "categoria inexistente")


def test_duplicate_institution(write_config):
    _bad(write_config, 'id = "inst-2"', 'id = "inst-1"', "instituição duplicada")


def test_duplicate_category(write_config):
    _bad(write_config, 'id = "b"', 'id = "a"', "categoria duplicada")


def test_stale_smaller_than_interval(write_config):
    _bad(write_config, "stale_after_minutes = 90", "stale_after_minutes = 59", "stale_after_minutes")


def test_non_loopback_host_rejected(write_config):
    _bad(write_config, '"127.0.0.1"', '"0.0.0.0"', "server.host")


def test_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BANKMON_CONFIG", str(tmp_path / "nao-existe.toml"))
    with pytest.raises(ConfigError, match="não encontrado"):
        load_config()


def test_invalid_toml(write_config):
    write_config("isto não é toml [")
    with pytest.raises(ConfigError, match="TOML inválido"):
        load_config()
