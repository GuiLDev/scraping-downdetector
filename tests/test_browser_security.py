"""Correcoes da revisao de seguranca da coleta pelo navegador (SEC-001 a SEC-011), TDD."""

from __future__ import annotations

import dataclasses
import re
import sqlite3
import threading
from datetime import timedelta
from urllib.parse import quote

import pytest
import waitress

from bankmon import browser_service, cli, config, db, runner
from bankmon.cli import EXIT_FAIL, EXIT_OK, main
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.config import ConfigError, PROJECT_ROOT, load_config
from bankmon.web import collect_api, create_app
from browser_helpers import ESTADO, URL, blocked_html, page_html
from test_browser_api import HEADERS, TOKEN, Env
from webutil import LOCAL_SECTION, MIN, NOW, cards, make_cfg

EXTERNAL = "https://exemplo.test/status"
SUSPENDED_NOTICE = ("Coleta suspensa: a fonte exibiu uma verificação. A coleta automática fica "
                    "encerrada até revisão da autorização.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def ext_env(tmp_path, monkeypatch):
    """Ambiente com origem EXTERNA autorizada, unica na lista (config montada direto: o TOML exige
    autorizacao registrada em docs, que hoje nao existe)."""
    e = Env(tmp_path, monkeypatch)
    e.cfg = dataclasses.replace(e.cfg, browser_origins=("https://exemplo.test",),
                                browser_authorization_ref="AUT-TESTE")
    e.app = create_app(e.cfg, clock=lambda: e.now, coleta_api=True, coleta_token=TOKEN)
    e.client = e.app.test_client()
    return e


def state(e, url=None):
    path = ESTADO if url is None else "/api/coleta/estado?alvo=" + quote(url, safe="")
    return e.client.get(path, headers=HEADERS).get_json()


# --- SEC-001: checagem de pausa, gravacao da execucao e do estado numa unica secao critica ----------


def _ingest(env, html, now, **kw):
    """Como a extensao: reserva a vaga e so entao envia a leitura (a reserva ja recusa se pausado)."""
    rid = browser_service.reserve(env.cfg, URL, now).reserva_id
    return browser_service.ingest(env.cfg, URL, html, now, reserva_id=rid, **kw)


def test_ingest_after_a_pause_is_refused_in_the_service_and_never_reactivates(env):
    _ingest(env, blocked_html(), NOW)
    with pytest.raises(browser_service.Paused):
        _ingest(env, page_html(), NOW + MIN)
    assert browser_service.read_state(env.cfg).estado == "pausado"
    assert env.statuses() == ["inaccessible"]  # a leitura recusada nao gravou execucao


def test_a_slow_ingest_holds_the_critical_section_so_a_pause_cannot_interleave(env):
    started, release = threading.Event(), threading.Event()

    class SlowOk(BrowserHtmlCollector):
        def collect(self, institutions):
            started.set()
            assert release.wait(10)
            return super().collect(institutions)

    outcome = {}

    def slow():
        outcome["a"] = _ingest(env, page_html(), NOW, collector_factory=SlowOk)

    t = threading.Thread(target=slow)
    t.start()
    try:
        assert started.wait(10)
        # leitura bloqueada chega no meio: nao entra (lock), nao pausa nem grava nada
        with pytest.raises(runner.LockBusy):
            _ingest(env, blocked_html(), NOW)
    finally:
        release.set()
        t.join(10)
    assert outcome["a"].estado == "ativo"
    _ingest(env, blocked_html(), NOW + MIN)  # agora sim, em serie
    assert browser_service.read_state(env.cfg).estado == "pausado"
    with pytest.raises(browser_service.Paused):
        _ingest(env, page_html(), NOW + 2 * MIN)
    assert browser_service.read_state(env.cfg).estado == "pausado"


def test_resume_waits_for_the_lock_instead_of_racing(env):
    env.post(blocked_html())
    csrf = env.csrf()
    with runner.file_lock(env.cfg.lock_path):
        assert env.client.post("/coleta/retomar", data={"csrf": csrf}).status_code == 503
    assert state(env)["estado"] == "pausado"


# --- SEC-002: origem revogada --------------------------------------------------------------------------


def test_state_endpoint_refuses_unauthorized_alvo_with_403(env):
    url = "/api/coleta/estado?alvo="
    assert env.client.get(url + "https://downdetector.com.br/", headers=HEADERS).status_code == 403
    assert env.client.get(url + "http://evil.test:8777/dd/", headers=HEADERS).status_code == 403
    assert env.client.get(url + "http://127.0.0.1:8777/dd/", headers=HEADERS).status_code == 200
    assert env.client.get(url + "http://127.0.0.1:9999/dd/", headers=HEADERS).status_code == 403  # porta nao listada


# --- SEC-003: trava de politica para origem NAO local --------------------------------------------------------


def test_loopback_origin_with_zero_interval_allows_back_to_back_readings(env):
    assert env.post(page_html()).status_code == 200
    assert env.post(page_html()).status_code == 200


def test_challenge_on_external_origin_suspends_and_resume_does_not_reactivate(ext_env):
    e = ext_env
    assert e.post(blocked_html(), url=EXTERNAL).get_json()["estado"] == "pausado"
    s = state(e, EXTERNAL)
    assert s["estado"] == "pausado" and s["motivo"] == "suspenso_por_desafio"
    html = e.home()
    assert SUSPENDED_NOTICE in html
    assert "Retomar verificação" not in html and "resume-form" not in html and "resolva" not in html.lower()
    csrf = e.app.template_context_processors[None][-1]()["csrf_token"]
    assert e.client.post("/coleta/retomar", data={"csrf": csrf}).status_code == 409
    assert state(e, EXTERNAL)["estado"] == "pausado"
    e.now = NOW + 120 * MIN
    assert e.post(page_html(), url=EXTERNAL).status_code == 409


def test_external_config_refuses_resume_even_for_a_leftover_local_pause(ext_env):
    """INF-01: banco reconfigurado de loopback para externa com a pausa de loopback ainda gravada."""
    _leftover_state_is_an_external_suspension(ext_env, "pausado", browser_service.BLOCK_REASON)


@pytest.mark.parametrize("motivo", [browser_service.RESUME_REASON, browser_service.SUSPENDED_REASON])
def test_external_config_treats_a_leftover_local_verification_as_a_suspension(ext_env, motivo):
    """INF-01 (SEC-016/SEC-022): `verificando` gravado nunca vale para a origem externa."""
    _leftover_state_is_an_external_suspension(ext_env, "verificando", motivo)


def _leftover_state_is_an_external_suspension(e, estado, motivo):
    conn = db.open_db(e.cfg)
    try:
        db.set_collector_state(conn, estado, motivo, NOW)
    finally:
        conn.close()
    html = e.home()
    assert SUSPENDED_NOTICE in html and "Verificação solicitada" not in html
    assert "Retomar verificação" not in html and "resume-form" not in html and "resolva" not in html.lower()
    csrf = e.app.template_context_processors[None][-1]()["csrf_token"]
    assert e.client.post("/coleta/retomar", data={"csrf": csrf}).status_code == 409
    s = state(e, EXTERNAL)
    assert (s["estado"], s["pode_reservar"]) == ("pausado", False)
    assert e.reserve(EXTERNAL).status_code == 409
    resp = e.client.post("/api/coleta", json={"url": EXTERNAL, "html": page_html(), "modo": "verificacao"},
                         headers=HEADERS)
    assert resp.status_code == 409 and e.statuses() == []


def test_local_challenge_keeps_the_manual_resume_flow(env):
    env.post(blocked_html())
    assert state(env)["motivo"] == "bloqueio_detectado"
    html = env.home()
    assert "Retomar verificação</button>" in html and "resolva manualmente" in html.lower()
    assert env.client.post("/coleta/retomar", data={"csrf": env.csrf()}).status_code == 303
    assert state(env)["estado"] == "verificando"


EXTERNAL_FAILURES_NOTICE = ("Coleta suspensa: 3 leituras seguidas sem sucesso em origem externa. "
                            "A coleta só volta após revisão da configuração e da autorização.")


def test_consecutive_failures_on_external_origin_suspend_and_refuse_resume(ext_env):
    e = ext_env
    for k in range(3):
        e.now = NOW + k * 60 * MIN  # origem externa: 1 reserva por hora
        assert e.post("<html><body></body></html>", url=EXTERNAL).status_code == 200
    s = state(e, EXTERNAL)
    assert s["estado"] == "pausado" and s["motivo"] == "falhas_consecutivas_externa"
    html = e.home()
    assert EXTERNAL_FAILURES_NOTICE in html
    assert "Retomar verificação" not in html and "resume-form" not in html
    csrf = e.app.template_context_processors[None][-1]()["csrf_token"]
    assert e.client.post("/coleta/retomar", data={"csrf": csrf}).status_code == 409
    assert state(e, EXTERNAL)["estado"] == "pausado"


def test_consecutive_failures_on_loopback_keep_the_manual_resume(env):
    e = env
    for _ in range(3):
        e.post("<html><body></body></html>")
    assert state(e)["motivo"] == "falhas_consecutivas"
    assert e.client.post("/coleta/retomar", data={"csrf": e.csrf()}).status_code == 303


DOC_SECTION = "## Autorizações registradas\n\n- `AUT-TESTE-1`: contrato de teste (escopo fictício)\n"


def _toml_with_ref(write_config, ref):
    from conftest import TOML

    return write_config(TOML.format(collector="demo") + (
        '\n[coleta_navegador]\norigens_autorizadas = ["https://exemplo.test"]\n'
        f'autorizacao_ref = "{ref}"\n'))


def test_autorizacao_ref_must_match_an_id_in_the_registered_authorizations(write_config, tmp_path, monkeypatch):
    doc = tmp_path / "doc.md"
    doc.write_text("# Doc\n" + DOC_SECTION + "\n## Outra\n- `AUT-FORA`: nao conta\n", "utf-8")
    monkeypatch.setattr(config, "AUTHORIZATIONS_DOC", doc)
    _toml_with_ref(write_config, "AUT-TESTE-1")
    assert load_config().browser_origins == ("https://exemplo.test",)
    for bad in ("AUT-FORA", "AUT-TESTE", "contrato 123"):
        _toml_with_ref(write_config, bad)
        with pytest.raises(ConfigError, match="Autorizações registradas"):
            load_config()


@pytest.mark.parametrize("listed", [
    '"http://127.0.0.1:8777", "https://exemplo.test"',
    '"https://exemplo.test", "https://outro.test"',
])
def test_external_origin_never_shares_the_collector_state_with_another_origin(
        write_config, tmp_path, monkeypatch, listed):
    """INF-01 (restricao temporaria): o estado da coleta e unico por banco."""
    from conftest import TOML

    doc = tmp_path / "doc.md"
    doc.write_text("# Doc\n" + DOC_SECTION, "utf-8")
    monkeypatch.setattr(config, "AUTHORIZATIONS_DOC", doc)
    write_config(TOML.format(collector="demo") + (
        f'\n[coleta_navegador]\norigens_autorizadas = [{listed}]\nautorizacao_ref = "AUT-TESTE-1"\n'))
    with pytest.raises(ConfigError, match="única origem"):
        load_config()


def test_with_the_real_docs_no_external_origin_is_accepted(write_config):
    _toml_with_ref(write_config, "qualquer-coisa")
    with pytest.raises(ConfigError, match="Autorizações registradas"):
        load_config()
    text = (PROJECT_ROOT / "docs" / "etapa2-viabilidade-coletor.md").read_text("utf-8")
    assert "## Autorizações registradas" in text


def test_extensao_requires_60_minutes_for_external_target(write_config, tmp_path, monkeypatch, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text(DOC_SECTION, "utf-8")
    monkeypatch.setattr(config, "AUTHORIZATIONS_DOC", doc)
    _toml_with_ref(write_config, "AUT-TESTE-1")
    cfg = load_config()
    code = main(["extensao", "--alvo", EXTERNAL, "--intervalo", "59"])
    assert code == EXIT_FAIL and "60" in capsys.readouterr().err
    assert not (cfg.data_dir / "extensao-coletor").exists()
    assert main(["extensao", "--alvo", EXTERNAL, "--intervalo", "60"]) == EXIT_OK


# --- SEC-004: alvo normalizado ------------------------------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("http://127.0.0.1:8777/dd", "http://127.0.0.1:8777/dd/"),
    ("http://localhost", "http://localhost/"),
    ("http://localhost:9100/a/b/", "http://localhost:9100/a/b/"),
])
def test_extensao_normalizes_target_to_end_with_slash(write_config, capsys, given, expected):
    from conftest import TOML

    section = ('\n[coleta_navegador]\norigens_autorizadas = '
               '["http://127.0.0.1:8777", "http://localhost", "http://localhost:9100"]\n')
    write_config(TOML.format(collector="demo") + section)
    cfg = load_config()
    assert main(["extensao", "--alvo", given]) == EXIT_OK
    import json
    assert json.loads((cfg.data_dir / "extensao-coletor" / "config.json").read_text("utf-8"))["alvo"] == expected


# --- SEC-005: sem a permissao "tabs" -------------------------------------------------------------------------------


def test_manifest_has_no_tabs_permission(write_config, capsys):
    import json
    from conftest import TOML
    write_config(TOML.format(collector="demo") + LOCAL_SECTION)
    assert sorted(json.loads((PROJECT_ROOT / "extension/coletor/manifest.json").read_text("utf-8"))["permissions"]) == [
        "alarms", "scripting", "storage"]
    cfg = load_config()
    main(["extensao", "--alvo", "http://127.0.0.1:8777/dd"])
    generated = json.loads((cfg.data_dir / "extensao-coletor" / "manifest.json").read_text("utf-8"))
    assert "tabs" not in generated["permissions"]


# --- SEC-006: JSON aninhado e 500 em JSON ---------------------------------------------------------------------------


def test_deeply_nested_json_is_400_json_not_500(env):
    body = b"[" * 200_000 + b"]" * 200_000
    resp = env.client.post("/api/coleta", data=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert resp.status_code == 400 and resp.is_json and "erro" in resp.get_json()


def test_unexpected_error_on_api_is_a_json_500_without_traceback(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("segredo interno /c/caminho")

    monkeypatch.setattr(browser_service, "ingest", boom)
    resp = env.post(page_html())
    assert resp.status_code == 500 and resp.is_json
    assert "segredo" not in resp.get_data(as_text=True) and "Traceback" not in resp.get_data(as_text=True)


# --- SEC-007: tamanho maximo no waitress ----------------------------------------------------------------------------


def test_serve_limits_request_body_size_in_waitress(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch, extra=LOCAL_SECTION)
    calls = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.update(kw=kw))
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_OK
    assert calls["kw"]["max_request_body_size"] == collect_api.MAX_BODY_BYTES


# --- SEC-008: falhas consecutivas pausam ----------------------------------------------------------------------------------


INCOMPLETE = "<html><body></body></html>"


def test_three_consecutive_non_ok_readings_pause_with_reason(env):
    for _ in range(2):
        assert env.post(INCOMPLETE).get_json()["estado"] == "ativo"
    assert env.post(INCOMPLETE).get_json()["estado"] == "pausado"
    s = state(env)
    assert s["motivo"] == "falhas_consecutivas"
    html = env.home()
    assert "Coleta pausada: 3 leituras seguidas sem sucesso" in html and "Retomar verificação</button>" in html
    assert env.post(page_html()).status_code == 409


def test_an_ok_reading_resets_the_failure_count(env):
    env.post(INCOMPLETE)
    env.post(INCOMPLETE)
    assert env.post(page_html()).get_json()["estado"] == "ativo"
    env.post(INCOMPLETE)
    assert env.post(INCOMPLETE).get_json()["estado"] == "ativo"


def test_after_resume_one_failure_does_not_pause_again_immediately(env):
    for _ in range(3):
        env.post(INCOMPLETE)
    env.now = NOW + 5 * MIN
    env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    env.now = NOW + 6 * MIN
    assert env.post(INCOMPLETE).get_json()["estado"] == "verificando"
    env.now = NOW + 7 * MIN
    assert env.post(page_html()).get_json()["estado"] == "ativo"


def test_failure_count_after_resume_is_exact_even_when_everything_happens_in_the_same_second(env):
    """INF-03: as falhas de antes da retomada nao contam e as de depois contam, no mesmo segundo."""
    for _ in range(3):
        env.post(INCOMPLETE)
    assert state(env)["estado"] == "pausado"
    assert env.client.post("/coleta/retomar", data={"csrf": env.csrf()}).status_code == 303
    assert env.post(INCOMPLETE).get_json()["estado"] == "verificando"
    assert env.post(INCOMPLETE).get_json()["estado"] == "verificando"
    resp = env.post(INCOMPLETE).get_json()
    assert resp["estado"] == "pausado" and state(env)["motivo"] == "falhas_consecutivas"


def test_a_block_and_the_resume_in_the_same_second_do_not_count_as_failures_after_it(env):
    env.post(blocked_html())
    assert env.client.post("/coleta/retomar", data={"csrf": env.csrf()}).status_code == 303
    assert env.post(INCOMPLETE).get_json()["estado"] == "verificando"
    assert env.post(INCOMPLETE).get_json()["estado"] == "verificando"


# --- SEC-018: banco v4 nunca aparece como "ativo" ----------------------------------------------------------------


def _v4_browser_db(cfg, estado, motivo):
    """Banco do navegador ainda na v4 (sem `apos_execucao`), como a versao anterior o deixou."""
    path = dataclasses.replace(cfg, collector="navegador").db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(db.SCHEMA_PATH.read_text("utf-8").split(db.V5_MARKER)[0] + "PRAGMA user_version = 4;")
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status) "
            "VALUES ('2026-10-29T12:00:00Z', '2026-10-29T12:00:00Z', 'navegador', 0, 'scheduled', 'inaccessible')")
        conn.execute("INSERT INTO collector_state VALUES (1, ?, ?, '2026-10-29T12:01:00Z', '2026-10-29T12:01:00Z')",
                     (estado, motivo))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("estado, motivo, shown", [
    ("pausado", browser_service.BLOCK_REASON, "Retomar verificação</button>"),
    ("verificando", browser_service.RESUME_REASON, "Verificação solicitada"),
])
def test_serve_navegador_migrates_a_v4_database_before_serving(tmp_path, monkeypatch, estado, motivo, shown):
    cfg = make_cfg(tmp_path, monkeypatch, extra=LOCAL_SECTION)
    path = _v4_browser_db(cfg, estado, motivo)
    calls = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.update(app=app))
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_OK
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT estado, motivo FROM collector_state").fetchone() == (estado, motivo)
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()
    client = calls["app"].test_client()
    token = (tmp_path / "data" / "coleta-token.txt").read_text("utf-8").strip()
    s = client.get(ESTADO, headers={"X-BankMonitor-Token": token}).get_json()
    assert s["estado"] == estado
    if estado == "pausado":
        assert s["pode_reservar"] is False
    html = client.get("/").get_data(as_text=True)
    assert shown in html and "ilegível" not in html


def test_serve_navegador_without_a_database_does_not_create_one(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, extra=LOCAL_SECTION)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_OK
    assert not dataclasses.replace(cfg, collector="navegador").db_path.exists()


def test_state_api_over_an_unmigrated_v4_database_fails_closed_without_writing(env):
    path = _v4_browser_db(env.cfg, "pausado", browser_service.BLOCK_REASON)
    before = path.read_bytes()
    resp = env.client.get(ESTADO, headers=HEADERS)
    assert resp.status_code == 500 and resp.get_json() == {"erro": "erro interno"}
    html = env.home()
    assert "ilegível" in html and not any("state-no-alert" in c for c in cards(html))
    assert path.read_bytes() == before


def test_availability_over_an_unmigrated_v4_database_raises_instead_of_allowing(env):
    """SEC-020: o fail-closed de `check_availability` tambem e coberto (a API le o estado antes)."""
    _v4_browser_db(env.cfg, "pausado", browser_service.BLOCK_REASON)
    with pytest.raises(sqlite3.OperationalError):
        browser_service.check_availability(env.cfg, URL, NOW)


def test_serve_navegador_reports_a_failed_startup_migration_without_traceback(tmp_path, monkeypatch, capsys):
    """SEC-019: outro `serve` ja migrou entre a leitura da versao e o ALTER (coluna ja existe)."""
    cfg = make_cfg(tmp_path, monkeypatch, extra=LOCAL_SECTION)
    path = _v4_browser_db(cfg, "pausado", browser_service.BLOCK_REASON)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE collector_state ADD COLUMN apos_execucao INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    called = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: called.append(1))
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_FAIL
    err = capsys.readouterr().err
    assert "Erro de banco" in err and "Traceback" not in err and called == []


def test_verification_of_an_external_url_is_refused_even_in_the_verifying_state(env):
    """SEC-021: a trava `external` da verificacao, independente do `effective_state`."""
    env.post(blocked_html())
    env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    assert state(env)["estado"] == "verificando"
    with pytest.raises(browser_service.VerificationRefused):
        browser_service.ingest(env.cfg, EXTERNAL, page_html(), NOW, verification=True)


# --- SEC-011: .gitignore -----------------------------------------------------------------------------------------------------


def test_gitignore_covers_extension_secrets():
    lines = (PROJECT_ROOT / ".gitignore").read_text("utf-8").splitlines()
    for rule in ("extension/**/config.json", "**/coleta-token.txt", "**/extensao-coletor/"):
        assert rule in lines, rule
