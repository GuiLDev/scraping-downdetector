"""Coleta pelo navegador, camada 2: lista explicita de origens (sem padrao implicito) e TOML (TDD)."""

from __future__ import annotations

import pytest

from bankmon import config, origins
from bankmon.config import ConfigError, load_config
from webutil import toml_text


LOCAL = ("http://127.0.0.1:8777",)


def test_listed_loopback_origin_matches_exactly_scheme_host_and_port():
    assert origins.is_authorized("http://127.0.0.1:8777/dd", LOCAL)
    for url in ("http://127.0.0.1:8778/dd", "http://localhost:8777/dd", "https://127.0.0.1:8777/dd",
                "http://127.0.0.1/dd"):
        assert not origins.is_authorized(url, LOCAL), url


@pytest.mark.parametrize("url", [
    "https://downdetector.com.br/", "http://127.0.0.1.evil.com:8777/", "http://evil.com/?h=127.0.0.1",
    "http://user:pw@127.0.0.1:8777/", "http://127.0.0.1@evil.com/", "file:///C:/x.html",
    "javascript:alert(1)", "", None, 42,
])
def test_listed_origins_refuse_everything_else(url):
    assert not origins.is_authorized(url, LOCAL)


def test_no_implicit_origins_and_empty_list_refuses_all():
    assert not hasattr(origins, "DEFAULT_ORIGINS")
    assert not origins.is_authorized("http://127.0.0.1:8777/dd", ())


def test_explicit_origin_pattern_matches_scheme_host_and_port():
    patterns = ("https://exemplo.test",)
    assert origins.is_authorized("https://exemplo.test/pagina", patterns)
    assert origins.is_authorized("https://exemplo.test:443/pagina", patterns)
    assert not origins.is_authorized("https://exemplo.test:8443/pagina", patterns)
    assert not origins.is_authorized("http://exemplo.test/pagina", patterns)
    assert not origins.is_authorized("https://sub.exemplo.test/", patterns)


def test_strip_query_drops_query_fragment_and_userinfo():
    assert origins.strip_query("http://127.0.0.1:8777/dd?a=1&token=x#frag") == "http://127.0.0.1:8777/dd"
    assert origins.strip_query("https://h.test") == "https://h.test/"


@pytest.mark.parametrize("bad", ["*", "http://127.0.0.1:*", "http://*.x.com", "ftp://x.com", "x.com", "http://x.com/path", "http://x.com:abc"])
def test_invalid_patterns_are_rejected(bad):
    with pytest.raises(ValueError):
        origins.parse_pattern(bad)


# --- TOML ---------------------------------------------------------------------------------


def _load(tmp_path, monkeypatch, extra=""):
    doc = tmp_path / "autorizacoes.md"  # as autorizacoes registradas de verdade (docs) nao existem
    doc.write_text("## Autorizações registradas\n\n- `AUT-1`: autorização fictícia dos testes\n", "utf-8")
    monkeypatch.setattr(config, "AUTHORIZATIONS_DOC", doc)
    path = tmp_path / "c.toml"
    path.write_text(toml_text() + extra, "utf-8")
    monkeypatch.setenv("BANKMON_CONFIG", str(path))
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def test_config_without_section_has_no_authorized_origin(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch)
    assert cfg.browser_origins == () and not cfg.browser_external
    assert cfg.browser_local_interval_s == 30


def test_loopback_origin_needs_no_authorization_ref(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, '\n[coleta_navegador]\norigens_autorizadas = ["http://127.0.0.1:8791"]\n')
    assert cfg.browser_origins == ("http://127.0.0.1:8791",) and not cfg.browser_external


def test_wildcard_port_is_a_config_error(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="origens_autorizadas"):
        _load(tmp_path, monkeypatch, '\n[coleta_navegador]\norigens_autorizadas = ["http://127.0.0.1:*"]\n')


@pytest.mark.parametrize("value,expected", [("0", 0), ("45", 45)])
def test_local_interval_is_configurable(tmp_path, monkeypatch, value, expected):
    cfg = _load(tmp_path, monkeypatch, f"\n[coleta_navegador]\nintervalo_minimo_local_s = {value}\n")
    assert cfg.browser_local_interval_s == expected


@pytest.mark.parametrize("value", ["-1", "true", '"30"', "1.5"])
def test_invalid_local_interval_is_a_config_error(tmp_path, monkeypatch, value):
    with pytest.raises(ConfigError, match="intervalo_minimo_local_s"):
        _load(tmp_path, monkeypatch, f"\n[coleta_navegador]\nintervalo_minimo_local_s = {value}\n")


def test_external_origin_requires_authorization_ref(tmp_path, monkeypatch):
    block = '\n[coleta_navegador]\norigens_autorizadas = ["https://exemplo.test"]\n'
    with pytest.raises(ConfigError, match="autorizacao_ref"):
        _load(tmp_path, monkeypatch, block)
    with pytest.raises(ConfigError, match="autorizacao_ref"):
        _load(tmp_path, monkeypatch, block + 'autorizacao_ref = "   "\n')


def test_external_origin_with_ref_is_listed(tmp_path, monkeypatch):
    block = ('\n[coleta_navegador]\norigens_autorizadas = ["https://exemplo.test"]\n'
             'autorizacao_ref = "AUT-1"\n')
    cfg = _load(tmp_path, monkeypatch, block)
    assert cfg.browser_origins == ("https://exemplo.test",)
    assert cfg.browser_external


def test_invalid_origin_pattern_in_toml_is_config_error(tmp_path, monkeypatch):
    block = '\n[coleta_navegador]\norigens_autorizadas = ["*"]\nautorizacao_ref = "x"\n'
    with pytest.raises(ConfigError, match="origens_autorizadas"):
        _load(tmp_path, monkeypatch, block)


def test_default_port_is_omitted_so_one_origin_has_one_key():
    assert origins.strip_query("https://h:443/x?a=1") == "https://h/x"
    assert origins.strip_query("http://localhost:80/") == "http://localhost/"
    assert origins.strip_query("http://127.0.0.1:8777/dd") == "http://127.0.0.1:8777/dd"
    assert origins.origin_of("https://h:443/x") == origins.origin_of("https://h/x") == "https://h"


def test_url_over_the_stored_limit_is_not_authorized():
    assert origins.MAX_URL_LEN == 480
    ok = "http://127.0.0.1:8777/" + "a" * 400
    assert origins.is_authorized(ok, LOCAL)
    assert not origins.is_authorized("http://127.0.0.1:8777/" + "a" * 500, LOCAL)
