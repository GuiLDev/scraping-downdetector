"""Leitura pontual: UMA leitura do DOM ja carregado, acionada a mao, pelo caminho normal da reserva (TDD).

Nao e autorizacao do servico: a config pontual aceita origem externa sem `autorizacao_ref` e recusa
qualquer `autorizacao_ref`. Nada aqui toca rede ou `data/` real (tmp_path).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from urllib.parse import quote

import pytest

from bankmon.cli import EXIT_FAIL, EXIT_OK, main
from bankmon.config import ConfigError, load_config
from browser_helpers import blocked_html, page_html
from test_browser_api import HEADERS, Env
from test_browser_reservation import rebuild
from webutil import LOCAL_SECTION

EXT = "https://exemplo.test"
EXT_URL = EXT + "/status"
BANNER = (
    "LEITURA PONTUAL — DOM da aba aberta pelo usuário, acionada manualmente; sem coleta automática"
)


def section(origins: list[str], extra: str = '\nleitura = "pontual"') -> str:
    listed = ", ".join(f'"{o}"' for o in origins)
    return f"\n[coleta_navegador]\norigens_autorizadas = [{listed}]{extra}\n"


@pytest.fixture
def toml(write_config):
    from conftest import TOML

    def _load(extra: str):
        write_config(TOML.format(collector="demo") + extra)
        return load_config()

    return _load


@pytest.fixture
def env(tmp_path, monkeypatch):
    return rebuild(Env(tmp_path, monkeypatch), browser_origins=(EXT,), browser_reading="pontual")


def reserve_pontual(e, url=EXT_URL):
    return e.client.post("/api/coleta/reserva", json={"alvo": url, "modo": "pontual"}, headers=HEADERS)


def pontual_post(e, html, url=EXT_URL, reserve=True, **body):
    payload = {"url": url, "html": html, **body}
    if reserve:
        payload["reserva_id"] = reserve_pontual(e, url).get_json()["reserva_id"]
    return e.client.post("/api/coleta", json=payload, headers=HEADERS)


def csrf(e):
    return e.app.template_context_processors[None][-1]()["csrf_token"]


# --- config -----------------------------------------------------------------------------------------


def test_config_accepts_one_external_origin_without_authorization_ref(toml):
    cfg = toml(section([EXT]))
    assert cfg.browser_reading == "pontual" and cfg.browser_pontual
    assert cfg.browser_origins == (EXT,) and cfg.browser_authorization_ref == ""


def test_config_defaults_to_the_automatic_reading(toml):
    cfg = toml(LOCAL_SECTION)
    assert cfg.browser_reading == "automatica" and not cfg.browser_pontual


def test_config_refuses_authorization_ref_in_pontual_mode(toml):
    with pytest.raises(ConfigError, match="autorizacao_ref"):
        toml(section([EXT], '\nleitura = "pontual"\nautorizacao_ref = "AUT-1"'))


def test_config_refuses_more_than_one_origin_in_pontual_mode(toml):
    with pytest.raises(ConfigError, match="exatamente uma"):
        toml(section([EXT, "http://127.0.0.1:8777"]))


def test_config_refuses_pontual_without_origin(toml):
    with pytest.raises(ConfigError, match="exatamente uma"):
        toml(section([]))


@pytest.mark.parametrize("value", ['"automatica"', '"outra"', "true", "1"])
def test_config_refuses_invalid_leitura(toml, value):
    with pytest.raises(ConfigError, match="leitura"):
        toml(section([EXT], f"\nleitura = {value}"))


def test_automatic_external_without_authorization_is_still_refused(toml):
    with pytest.raises(ConfigError, match="autorizacao_ref"):
        toml(section([EXT], ""))


# --- extensao ---------------------------------------------------------------------------------------


def run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def generated(cfg):
    folder = cfg.data_dir / "extensao-coletor"
    return (json.loads((folder / "config.json").read_text("utf-8")),
            json.loads((folder / "manifest.json").read_text("utf-8")))


def test_extensao_pontual_writes_the_flag_and_does_not_require_60_minutes(toml, capsys):
    cfg = toml(section([EXT]))
    code, _, _ = run(["extensao", "--pontual", "--alvo", EXT_URL], capsys)
    conf, manifest = generated(cfg)
    assert code == EXIT_OK and conf["pontual"] is True and conf["alvo"] == EXT_URL + "/"
    assert manifest["host_permissions"] == ["http://127.0.0.1:8765/*", EXT + "/*"]


def test_extensao_pontual_with_a_normal_config_is_refused(toml, capsys):
    cfg = toml(LOCAL_SECTION)
    code, _, err = run(["extensao", "--pontual", "--alvo", "http://127.0.0.1:8777/dd"], capsys)
    assert code == EXIT_FAIL and "pontual" in err
    assert not (cfg.data_dir / "extensao-coletor").exists()


def test_extensao_without_pontual_with_a_pontual_config_is_refused(toml, capsys):
    cfg = toml(section([EXT]))
    code, _, err = run(["extensao", "--alvo", EXT_URL, "--intervalo", "60"], capsys)
    assert code == EXIT_FAIL and "--pontual" in err
    assert not (cfg.data_dir / "extensao-coletor").exists()


def test_normal_extension_config_has_no_pontual_flag(toml, capsys):
    cfg = toml(LOCAL_SECTION)
    assert run(["extensao", "--alvo", "http://127.0.0.1:8777/dd"], capsys)[0] == EXIT_OK
    conf, manifest = generated(cfg)
    assert "pontual" not in conf and manifest["name"] == "BankMonitor — Coleta Automática"


# --- API com config pontual -------------------------------------------------------------------------


def test_pontual_reading_with_a_valid_reservation_records_the_run(env, caplog):
    with caplog.at_level(logging.INFO, logger="bankmon.browser"):
        resp = pontual_post(env, page_html(), modo="pontual")
    assert resp.status_code == 200 and resp.get_json()["estado"] == "ativo"
    assert env.statuses() == ["ok"]
    assert env.rows("SELECT url FROM run_sources") == [(EXT_URL,)]
    assert env.rows("SELECT consumed_at IS NOT NULL FROM collector_reservations") == [(1,)]
    assert any("leitura pontual" in r.getMessage() for r in caplog.records)


def test_pontual_config_without_modo_is_409(env):
    resp = pontual_post(env, page_html())
    assert resp.status_code == 409 and "pontual" in resp.get_json()["erro"]
    assert env.statuses() == []


def test_pontual_config_refuses_verificacao_with_409(env):
    resp = pontual_post(env, page_html(), reserve=False, modo="verificacao")
    assert resp.status_code == 409 and not env.cfg.db_path.exists()


def test_pontual_without_reservation_is_429(env):
    resp = pontual_post(env, page_html(), reserve=False, modo="pontual")
    assert resp.status_code == 429


def test_a_second_reservation_before_60_minutes_is_429(env):
    assert pontual_post(env, page_html(), modo="pontual").status_code == 200
    assert reserve_pontual(env).status_code == 429
    env.now = env.now + timedelta(minutes=30)
    assert reserve_pontual(env).status_code == 429


def test_a_challenge_dom_suspends_the_origin_and_resume_is_409(env):
    resp = pontual_post(env, blocked_html(), modo="pontual")
    assert resp.status_code == 200 and env.statuses() == ["inaccessible"]
    state = env.client.get("/api/coleta/estado?alvo=" + quote(EXT_URL, safe=""), headers=HEADERS).get_json()
    assert state["estado"] == "pausado" and state["motivo"] == "suspenso_por_desafio"
    assert env.client.post("/coleta/retomar", data={"csrf": csrf(env), "origem": EXT}).status_code == 409


def test_pontual_still_checks_token_and_origin(env):
    body = {"url": EXT_URL, "html": page_html(), "modo": "pontual"}
    assert env.client.post("/api/coleta", json=body).status_code == 401
    other = {**body, "url": "https://outra.test/x"}
    assert env.client.post("/api/coleta", json=other, headers=HEADERS).status_code == 403


# --- API com config normal --------------------------------------------------------------------------


def test_modo_pontual_with_a_normal_config_is_400(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    url = "http://127.0.0.1:8777/dd"
    resp = pontual_post(e, page_html(), url=url, reserve=False, reserva_id=e.reserve(url).get_json()["reserva_id"],
                        modo="pontual")
    assert resp.status_code == 400 and "modo desconhecido" in resp.get_json()["erro"]
    assert e.statuses() == []


# --- dashboard --------------------------------------------------------------------------------------


def test_dashboard_shows_the_pontual_banner_and_on_demand_next_check(env):
    assert pontual_post(env, page_html(), modo="pontual").status_code == 200
    html = env.home()
    assert BANNER in html and "sob demanda" in html
    assert "COLETA PELO NAVEGADOR" not in html and "SIMULAÇÃO LOCAL" not in html


def test_dashboard_pontual_banner_shows_even_without_data(env):
    html = env.home()
    assert BANNER in html and "sob demanda" in html


# --- revisao de seguranca: SEC-001, SEC-003, SEC-006 ---------------------------------------------------


def reserve_rows(e):
    return e.rows("SELECT COUNT(*) FROM collector_reservations")


def test_pontual_reservation_requires_modo_pontual_and_writes_nothing_otherwise(env):
    for body in ({"alvo": EXT_URL}, {"alvo": EXT_URL, "modo": "verificacao"}, {"alvo": EXT_URL, "modo": "x"}):
        assert env.client.post("/api/coleta/reserva", json=body, headers=HEADERS).status_code == 409
    assert not env.cfg.db_path.exists()


def test_pontual_reservation_with_modo_pontual_is_200(env):
    resp = env.client.post("/api/coleta/reserva", json={"alvo": EXT_URL, "modo": "pontual"}, headers=HEADERS)
    assert resp.status_code == 200 and reserve_rows(env) == [(1,)]


def test_modo_pontual_in_a_reservation_with_a_normal_config_is_400(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    resp = e.client.post("/api/coleta/reserva", json={"alvo": "http://127.0.0.1:8777/dd", "modo": "pontual"},
                         headers=HEADERS)
    assert resp.status_code == 400 and "modo desconhecido" in resp.get_json()["erro"]
    assert e.reserve("http://127.0.0.1:8777/dd").status_code == 200  # sem modo: fluxo automatico intacto


def test_extensao_pontual_manifest_has_no_alarms_and_config_has_no_interval(toml, capsys):
    cfg = toml(section([EXT]))
    code, out, _ = run(["extensao", "--pontual", "--alvo", EXT_URL], capsys)
    conf, manifest = generated(cfg)
    assert code == EXIT_OK and "intervalo_min" not in conf
    assert "alarms" not in manifest["permissions"] and sorted(manifest["permissions"]) == ["scripting", "storage"]
    assert "manual" in manifest["description"] and "recarrega" not in manifest["description"].lower()
    assert manifest["name"] == "BankMonitor — Leitura Pontual"
    assert "intervalo" not in out and "coletarAgora()" in out


def test_extensao_pontual_with_explicit_interval_is_refused(toml, capsys):
    cfg = toml(section([EXT]))
    code, _, err = run(["extensao", "--pontual", "--alvo", EXT_URL, "--intervalo", "60"], capsys)
    assert code == EXIT_FAIL and "--intervalo" in err
    assert not (cfg.data_dir / "extensao-coletor").exists()
