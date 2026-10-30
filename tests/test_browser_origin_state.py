"""INF-01 definitivo (schema v6): estado e contador de falhas da coleta POR ORIGEM (TDD)."""

from __future__ import annotations

import dataclasses
from urllib.parse import quote

import pytest

from bankmon import browser_service, db, runner
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.web import create_app
from browser_helpers import URL, blocked_html, page_html
from test_browser_api import HEADERS, TOKEN, Env
from test_browser_reservation import rebuild
from webutil import MIN, NOW

A = "http://127.0.0.1:8777"
B = "http://localhost:9100"
URL_A = URL  # http://127.0.0.1:8777/dd
URL_B = B + "/dd"
EXT = "https://exemplo.test"
EXT_URL = EXT + "/status"
INCOMPLETE = "<html><body></body></html>"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def two(env):
    """Duas origens loopback na mesma config (intervalo local 0)."""
    return rebuild(env, browser_origins=(A, B))


def state(e, url):
    return e.client.get("/api/coleta/estado?alvo=" + quote(url, safe=""), headers=HEADERS).get_json()


def csrf(e):
    """Token CSRF direto do contexto (a dashboard so mostra o formulario com origem retomavel)."""
    return e.app.template_context_processors[None][-1]()["csrf_token"]


def resume(e, origem=None, client=None):
    data = {"csrf": csrf(e)}
    if origem is not None:
        data["origem"] = origem
    return (client or e.client).post("/coleta/retomar", data=data)


def origin_rows(e):
    return e.rows("SELECT origin, estado, motivo, falhas_seguidas FROM collector_origin_state ORDER BY origin")


def estado_of(resp):
    return resp.get_json()["estado"]


# --- contador e pausa por origem --------------------------------------------------------------------


def test_a_pause_of_one_origin_does_not_affect_another(two):
    assert estado_of(two.post(blocked_html(), url=URL_A)) == "pausado"
    assert state(two, URL_A)["estado"] == "pausado"
    assert state(two, URL_B)["estado"] == "ativo" and state(two, URL_B)["pode_reservar"] is True
    assert two.reserve(URL_A).status_code == 409
    rid = two.reserve(URL_B).get_json()["reserva_id"]
    assert estado_of(two.post(page_html(), url=URL_B, reserva_id=rid)) == "ativo"


def test_interleaved_failures_in_the_same_second_pause_only_origin_a_on_its_third(two):
    seen = [estado_of(two.post(INCOMPLETE, url=u)) for u in (URL_A, URL_B, URL_A, URL_B, URL_A)]
    assert seen == ["ativo", "ativo", "ativo", "ativo", "pausado"]
    assert state(two, URL_A)["motivo"] == "falhas_consecutivas"
    assert state(two, URL_B)["estado"] == "ativo"
    assert origin_rows(two) == [
        (A, "pausado", "falhas_consecutivas", 3), (B, "ativo", None, 2)]


def test_the_failure_counter_survives_a_restart(env):
    for _ in range(2):
        assert estado_of(env.post(INCOMPLETE)) == "ativo"
    restarted = create_app(env.cfg, clock=lambda: env.now, coleta_api=True, coleta_token=TOKEN)
    env.app, env.client = restarted, restarted.test_client()
    assert estado_of(env.post(INCOMPLETE)) == "pausado"


def test_state_and_run_are_recorded_atomically(env, monkeypatch):
    for _ in range(2):
        env.post(INCOMPLETE)
    rid = env.reserve().get_json()["reserva_id"]
    original = db.insert_transitions

    def boom(*a, **k):
        raise RuntimeError("falha no meio da gravacao")

    monkeypatch.setattr(db, "insert_transitions", boom)
    assert env.post(INCOMPLETE, reserva_id=rid).status_code == 500
    assert env.statuses() == ["error", "error"]  # a 3a execucao nao ficou gravada
    assert origin_rows(env) == [(A, "ativo", None, 2)]  # nem o estado: segue ativo com 2 falhas
    monkeypatch.setattr(db, "insert_transitions", original)
    assert estado_of(env.post(INCOMPLETE, reserva_id=rid)) == "pausado"  # a reserva tambem voltou atras
    assert env.statuses() == ["error"] * 3 and origin_rows(env)[0][1:] == ("pausado", "falhas_consecutivas", 3)


def test_two_configs_on_the_same_data_dir_have_independent_states(env):
    cfg_a = dataclasses.replace(env.cfg, browser_origins=(A,))
    cfg_b = dataclasses.replace(env.cfg, browser_origins=(B,))
    client_a, client_b = (
        create_app(c, clock=lambda: NOW, coleta_api=True, coleta_token=TOKEN).test_client() for c in (cfg_a, cfg_b))

    def post(client, url, html):
        rid = client.post("/api/coleta/reserva", json={"alvo": url}, headers=HEADERS).get_json()["reserva_id"]
        return client.post("/api/coleta", json={"url": url, "html": html, "reserva_id": rid}, headers=HEADERS)

    assert estado_of(post(client_a, URL_A, blocked_html())) == "pausado"
    assert estado_of(post(client_b, URL_B, page_html())) == "ativo"
    assert client_b.get("/api/coleta/estado?alvo=" + quote(URL_B, safe=""), headers=HEADERS).get_json()["estado"] == "ativo"
    assert client_a.get("/api/coleta/estado?alvo=" + quote(URL_A, safe=""), headers=HEADERS).get_json()["estado"] == "pausado"


def test_loopback_and_external_origins_in_one_database_are_independent(env):
    # config montada direto: o TOML recusa a combinacao (INF-01 temporaria), o que e esperado
    e = rebuild(env, browser_origins=(A, EXT), browser_authorization_ref="AUT-TESTE")
    assert estado_of(e.post(blocked_html(), url=URL_A)) == "pausado"
    assert state(e, EXT_URL)["estado"] == "ativo"  # a pausa loopback nao suspende a externa
    assert estado_of(e.post(page_html(), url=EXT_URL)) == "ativo"
    assert resume(e, A).status_code == 303
    assert state(e, URL_A)["estado"] == "verificando"
    assert resume(e, EXT).status_code == 409


# --- retomada por origem ---------------------------------------------------------------------------------


@pytest.mark.parametrize("origem", [
    None, "", "http://evil.test", "http://127.0.0.1:8777/", "HTTP://127.0.0.1:8777",
    "http://127.0.0.1:8777/dd", "http://127.0.0.1:9999", " http://127.0.0.1:8777", "http://127.0.0.1:8777 "])
def test_resume_with_many_origins_needs_an_exact_listed_origin(two, origem):
    two.post(blocked_html(), url=URL_A)
    before = origin_rows(two)
    assert resume(two, origem).status_code == 403
    assert origin_rows(two) == before and state(two, URL_A)["estado"] == "pausado"


def test_resuming_one_origin_does_not_change_another(two):
    two.post(blocked_html(), url=URL_A)
    two.post(blocked_html(), url=URL_B)
    assert resume(two, A).status_code == 303
    assert state(two, URL_A)["estado"] == "verificando" and state(two, URL_B)["estado"] == "pausado"
    assert origin_rows(two) == [(A, "verificando", browser_service.RESUME_REASON, 0), (B, "pausado", "bloqueio_detectado", 1)]


def test_resume_of_an_external_origin_is_409_even_when_active_and_writes_nothing(env):
    e = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    assert resume(e, EXT).status_code == 409 and not e.cfg.db_path.exists()  # sem banco: nada criado
    assert estado_of(e.post(page_html(), url=EXT_URL)) == "ativo"
    before = (origin_rows(e), e.statuses())
    assert resume(e, EXT).status_code == 409
    assert resume(e).status_code == 409  # sem `origem` e uma so configurada: a externa, tambem recusada
    assert (origin_rows(e), e.statuses()) == before


def test_a_legacy_global_pause_applies_to_origins_without_their_own_row(two):
    conn = db.open_db(two.cfg)
    try:
        conn.execute(
            "INSERT INTO collector_state (id, estado, motivo, desde, atualizado_em) "
            "VALUES (1, 'pausado', 'bloqueio_detectado', '2026-10-28T15:00:00Z', '2026-10-28T15:00:00Z')")
    finally:
        conn.close()
    assert state(two, URL_A)["estado"] == state(two, URL_B)["estado"] == "pausado"
    assert resume(two, A).status_code == 303
    assert origin_rows(two) == [(A, "verificando", browser_service.RESUME_REASON, 0)]  # criou so a linha de A
    assert state(two, URL_A)["estado"] == "verificando" and state(two, URL_B)["estado"] == "pausado"
    legacy = two.rows("SELECT estado, motivo FROM collector_state")
    assert legacy == [("pausado", "bloqueio_detectado")]  # a linha legada segue congelada


def test_runner_on_record_receives_the_validated_result(env):
    """`on_record(conn, result)` e chamado dentro da transacao com o resultado gravado."""
    seen = []
    conn = db.open_db(env.cfg)
    try:
        runner.collect_and_record(
            conn, env.cfg, BrowserHtmlCollector(page_html(), URL_A, NOW), "scheduled", lambda: NOW,
            lambda c, result: seen.append((c.in_transaction, result.source_status)))
    finally:
        conn.close()
    assert seen == [(True, "ok")]


# --- dashboard: um aviso por origem fora de `ativo` e um botao por origem retomavel -----------------------


def test_dashboard_shows_one_notice_per_origin_and_a_button_only_for_the_resumable_one(two):
    two.post(blocked_html(), url=URL_A)  # A: pausada por bloqueio (retomavel)
    two.post(blocked_html(), url=URL_B)
    assert resume(two, B).status_code == 303  # B: em verificacao (sem botao)
    html = two.home()
    assert f"Origem {A}: Coleta pausada: bloqueio detectado" in html
    assert f"Origem {B}: Verificação solicitada; aguardando a próxima leitura da aba." in html
    assert html.count('class="resume-form"') == 1 and html.count("Retomar verificação</button>") == 1
    assert f'name="origem" value="{A}"' in html and f'name="origem" value="{B}"' not in html
    assert f'<span class="muted">{A}</span>' in html


def test_dashboard_with_one_origin_keeps_the_plain_text_and_sends_the_hidden_origin(env):
    env.post(blocked_html())
    html = env.home()
    assert "Origem " not in html and '<span class="muted">http' not in html
    assert "Coleta pausada: bloqueio detectado" in html and "Retomar verificação</button>" in html
    assert f'name="origem" value="{A}"' in html
    assert env.client.post("/coleta/retomar", data={"csrf": env.csrf(), "origem": A}).status_code == 303
    assert state(env, URL_A)["estado"] == "verificando"


# --- SEC-024: a suspensao externa nao some trocando a origem externa --------------------------------------

SUSPENDED = "Coleta suspensa: a fonte exibiu uma verificação."


def test_external_suspension_follows_the_database_to_a_different_external_origin(env):
    x = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    assert estado_of(x.post(blocked_html(), url=EXT_URL)) == "pausado"
    other = "https://www.exemplo.test"
    y = rebuild(env, browser_origins=(other,), browser_authorization_ref="AUT-TESTE")  # mesmo banco
    s = state(y, other + "/status")
    assert (s["estado"], s["motivo"], s["pode_reservar"]) == ("pausado", "suspenso_por_desafio", False)
    assert y.reserve(other + "/status").status_code == 409
    before = y.statuses()
    assert y.post(page_html(), url=other + "/status").status_code == 409
    assert resume(y, other).status_code == 409
    assert y.statuses() == before and origin_rows(y) == [(EXT, "pausado", "suspenso_por_desafio", 1)]
    html = y.home()
    assert SUSPENDED in html and "Retomar verificação" not in html and "resume-form" not in html


def test_external_suspension_also_holds_for_an_external_origin_that_already_has_its_own_row(env):
    """SEC-028: A le `ok` (linha propria ativa), B e suspensa, a config volta para A: A fica suspensa."""
    other = "https://www.exemplo.test"
    a = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    assert estado_of(a.post(page_html(), url=EXT_URL)) == "ativo"
    b = rebuild(env, browser_origins=(other,), browser_authorization_ref="AUT-TESTE")
    assert estado_of(b.post(blocked_html(), url=other + "/status")) == "pausado"
    back = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    back.now = NOW + 120 * MIN
    s = state(back, EXT_URL)
    assert (s["estado"], s["motivo"], s["pode_reservar"]) == ("pausado", "suspenso_por_desafio", False)
    before = back.statuses()
    assert back.reserve(EXT_URL).status_code == 409
    assert back.post(page_html(), url=EXT_URL).status_code == 409 and back.statuses() == before


def test_external_suspension_does_not_suspend_a_loopback_origin(env):
    x = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    x.post(blocked_html(), url=EXT_URL)
    loop = rebuild(env, browser_origins=(A,))
    assert state(loop, URL_A)["estado"] == "ativo"


# --- lacunas: atomicidade do estado, resultado sanitizado, ordem do CSRF ----------------------------------


def test_a_failing_state_write_rolls_back_the_run_and_keeps_the_reservation(env, monkeypatch):
    rid = env.reserve().get_json()["reserva_id"]
    original = db.write_origin_state

    def boom(*a, **k):
        raise RuntimeError("falha ao gravar o estado")

    monkeypatch.setattr(db, "write_origin_state", boom)
    assert env.post(page_html(), reserva_id=rid).status_code == 500
    assert env.statuses() == [] and origin_rows(env) == []  # nada gravado
    monkeypatch.setattr(db, "write_origin_state", original)
    assert env.post(page_html(), reserva_id=rid).status_code == 200  # a mesma reserva ainda vale


def test_the_state_counter_uses_the_sanitized_result(env):
    class Malformed(BrowserHtmlCollector):
        def collect(self, institutions):
            return dataclasses.replace(super().collect(institutions), collector="outro")  # `ok` invalido

    rid = browser_service.reserve(env.cfg, URL_A, NOW).reserva_id
    browser_service.ingest(env.cfg, URL_A, page_html(), NOW, reserva_id=rid, collector_factory=Malformed)
    assert env.statuses() == ["error"]  # o runner sanitizou o `ok`
    assert origin_rows(env) == [(A, "ativo", None, 1)]  # e o contador viu `error`, nao `ok`


def test_a_wrong_csrf_is_403_before_the_origin_is_looked_at(env):
    e = rebuild(env, browser_origins=(EXT,), browser_authorization_ref="AUT-TESTE")
    resp = e.client.post("/coleta/retomar", data={"csrf": "errado", "origem": EXT})
    assert resp.status_code == 403 and not e.cfg.db_path.exists()
