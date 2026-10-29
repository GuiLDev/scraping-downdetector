"""Coleta pelo navegador, camada 3: API Flask + dashboard (TDD; Flask test client e tmp_path)."""

from __future__ import annotations

import dataclasses
import logging
import re
import sqlite3

import pytest

from bankmon import db, runner
from bankmon.web import create_app
from browser_helpers import ESTADO, URL, blocked_html, page_html
from webutil import MIN, NOW, cards, make_cfg

TOKEN = "tok-" + "x" * 40
HEADERS = {"X-BankMonitor-Token": TOKEN}
BANNER = "COLETA PELO NAVEGADOR — SIMULAÇÃO LOCAL (páginas de teste, não é o Downdetector)"


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.cfg = dataclasses.replace(
            make_cfg(tmp_path, monkeypatch), collector="navegador",
            browser_origins=("http://127.0.0.1:8777",), browser_local_interval_s=0)
        self.now = NOW
        self.app = create_app(self.cfg, clock=lambda: self.now, coleta_api=True, coleta_token=TOKEN)
        self.client = self.app.test_client()

    def reserve(self, url=URL, token=TOKEN):
        return self.client.post("/api/coleta/reserva", json={"alvo": url}, headers={"X-BankMonitor-Token": token})

    def post(self, html, url=URL, token=TOKEN, reserve=True, reserva_id=None, **kw):
        """Envia a leitura; por padrao reserva a vaga antes (como a extensao), se o token for valido."""
        body = {"url": url, "html": html}
        if reserva_id is None and reserve and token == TOKEN:
            booked = self.reserve(url)
            if booked.status_code == 200:
                reserva_id = booked.get_json()["reserva_id"]
        if reserva_id is not None:
            body["reserva_id"] = reserva_id
        headers = {"X-BankMonitor-Token": token} if token is not None else {}
        return self.client.post("/api/coleta", json=body, headers=headers, **kw)

    def home(self) -> str:
        resp = self.client.get("/")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def csrf(self) -> str:
        return re.search(r'name="csrf" value="([^"]+)"', self.home()).group(1)

    def rows(self, sql, params=()):
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def statuses(self):
        return [r[0] for r in self.rows("SELECT source_status FROM runs ORDER BY id")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# --- autenticacao, tamanho e origem ------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "errado", TOKEN + "x", TOKEN[:-1]])
def test_ingest_without_or_with_wrong_token_is_401_and_writes_nothing(env, token):
    assert env.post(page_html(), token=token).status_code == 401
    assert not env.cfg.db_path.exists()


def test_state_endpoint_requires_token(env):
    assert env.client.get("/api/coleta/estado").status_code == 401
    assert env.client.get(ESTADO, headers={"X-BankMonitor-Token": "x"}).status_code == 401


def test_state_endpoint_requires_alvo_and_checks_it_against_the_list(env):
    assert env.client.get("/api/coleta/estado", headers=HEADERS).status_code == 400
    assert env.client.get("/api/coleta/estado?alvo=", headers=HEADERS).status_code == 400
    assert env.client.get("/api/coleta/estado?alvo=http%3A%2F%2Fevil.test%2F", headers=HEADERS).status_code == 403
    assert env.client.get(ESTADO, headers=HEADERS).status_code == 200


def test_without_any_listed_origin_the_api_answers_403(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="navegador")
    client = create_app(cfg, clock=lambda: NOW, coleta_api=True, coleta_token=TOKEN).test_client()
    assert client.get(ESTADO, headers=HEADERS).status_code == 403
    assert client.post("/api/coleta", json={"url": URL, "html": page_html()}, headers=HEADERS).status_code == 403


def test_body_over_limit_is_413(env):
    resp = env.post("x" * (7 * 1024 * 1024), reserve=False)
    assert resp.status_code == 413 and not env.cfg.db_path.exists()


@pytest.mark.parametrize("url", [
    "https://downdetector.com.br/", "http://evil.test:8777/dd", "file:///C:/x.html", "", None, 7,
])
def test_unauthorized_origin_is_403_and_writes_nothing(env, url):
    assert env.post(page_html(), url=url).status_code == 403
    assert not env.cfg.db_path.exists()


@pytest.mark.parametrize("body", [b"nao e json", b"[1]", b'{"url": "http://127.0.0.1:1/x"}',
                                  b'{"url": "http://127.0.0.1:1/x", "html": 5}'])
def test_malformed_body_is_400(env, body):
    resp = env.client.post("/api/coleta", data=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert resp.status_code == 400 and not env.cfg.db_path.exists()


# --- ingestao ok ---------------------------------------------------------------------------------


def test_ok_ingestion_records_in_browser_db_only_and_answers_json(env):
    resp = env.post(page_html(alfa="enfrenta problemas"))
    body = resp.get_json()
    assert resp.status_code == 200
    assert (body["estado"], body["page_state"], body["servicos"]) == ("ativo", "ok", 12)
    assert body["run_id"] == 1 and set(body) == {"estado", "run_id", "page_state", "servicos"}
    assert env.statuses() == ["ok"]
    assert env.rows("SELECT kind, url FROM run_sources") == [("browser", URL)]
    svg = env.rows("SELECT svg_viewbox, svg_path_d FROM run_services WHERE institution_id = 'alfa'")
    assert svg == [("0 0 243 40", "M0 30 L10 20 L20 25")]
    produced = {p.name for p in env.cfg.data_dir.iterdir() if p.is_file()}
    assert {n for n in produced if n.endswith(".db")} == {"bankmon-navegador.db"}
    assert not any("demo" in n or "offline" in n or n == "bankmon.db" for n in produced)


def test_url_query_string_is_never_stored(env):
    assert env.post(page_html(), url=URL + "?sessao=abc&x=1").status_code == 200
    assert env.rows("SELECT url FROM run_sources") == [(URL,)]


def test_dashboard_shows_banner_states_and_charts_after_ok_ingestion(env):
    env.post(page_html(alfa="enfrenta problemas", beta="possíveis problemas"))
    html = env.home()
    assert BANNER in html
    by = {name: c for name in ("Banco Alfa", "Banco Beta", "Banco Gama")
          for c in cards(html) if f">{name}</a>" in c}
    assert "state-critical" in by["Banco Alfa"] and "state-attention" in by["Banco Beta"]
    assert "state-no-alert" in by["Banco Gama"] and 'd="M0 30 L10 20 L20 25"' in by["Banco Gama"]
    assert "Coleta pausada" not in html and "Última leitura válida (desatualizada)" not in html


def test_incomplete_page_is_error_run_and_does_not_pause(env):
    body = env.post("<html><body></body></html>").get_json()
    assert (body["estado"], body["page_state"]) == ("ativo", "incomplete")
    assert env.statuses() == ["error"]
    assert env.client.get(ESTADO, headers=HEADERS).get_json()["estado"] == "ativo"


def test_state_endpoint_reports_ativo_by_default(env):
    resp = env.client.get(ESTADO, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"estado": "ativo", "motivo": None, "desde": None,
                               "pode_reservar": True, "proxima_leitura_em": None}


def test_ingestion_while_another_collection_runs_is_503(env):
    with runner.file_lock(env.cfg.lock_path):
        assert env.post(page_html()).status_code == 503
    assert not env.cfg.db_path.exists() or env.statuses() == []


# --- bloqueio: pausa ---------------------------------------------------------------------------------


def _block_after_ok(env):
    env.now = NOW - 10 * MIN
    env.post(page_html())  # leitura valida 12:20 (America/Sao_Paulo)
    env.now = NOW
    return env.post(blocked_html())


def test_blocked_page_pauses_collection_and_records_inaccessible_run(env):
    resp = _block_after_ok(env)
    assert resp.status_code == 200
    body = resp.get_json()
    assert (body["estado"], body["page_state"], body["servicos"]) == ("pausado", "blocked_by_cloudflare", 0)
    assert env.statuses() == ["ok", "inaccessible"]
    state = env.client.get(ESTADO, headers=HEADERS).get_json()
    assert state["estado"] == "pausado" and "bloqueio" in state["motivo"]
    assert state["desde"] == "2026-10-28T15:30:00Z"


def test_paused_dashboard_warns_offers_resume_and_never_shows_green_for_old_data(env):
    _block_after_ok(env)
    html = env.home()
    assert ("Coleta pausada: bloqueio detectado às 12:30. Resolva manualmente na aba do navegador e "
            "clique em Retomar verificação. Os dados exibidos estão desatualizados.") in html
    assert 'action="/coleta/retomar"' in html and "Retomar verificação</button>" in html
    all_cards = cards(html)
    assert all_cards and all("state-no-data" in c for c in all_cards)
    assert not any("state-no-alert" in c or "state-attention" in c or "state-critical" in c for c in all_cards)
    gama = next(c for c in all_cards if ">Banco Gama</a>" in c)
    assert "Última leitura válida (desatualizada): Sem alerta em 28/10 às 12:20" in gama
    assert "<svg" not in gama  # grafico antigo nao contradiz o cartao


def test_ingestion_while_paused_is_409_and_writes_nothing(env):
    _block_after_ok(env)
    resp = env.post(page_html())
    assert resp.status_code == 409
    assert env.statuses() == ["ok", "inaccessible"]


# --- retomar -------------------------------------------------------------------------------------------


def test_resume_without_or_with_bad_csrf_is_refused_and_keeps_paused(env):
    _block_after_ok(env)
    assert env.client.post("/coleta/retomar").status_code in (400, 403)
    assert env.client.post("/coleta/retomar", data={"csrf": "errado"}).status_code == 403
    assert env.client.get(ESTADO, headers=HEADERS).get_json()["estado"] == "pausado"


def test_resume_sets_verifying_and_redirects_with_303(env):
    _block_after_ok(env)
    resp = env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    assert resp.status_code == 303 and resp.headers["Location"].endswith("/")
    assert env.client.get(ESTADO, headers=HEADERS).get_json()["estado"] == "verificando"
    html = env.home()
    assert "Verificação solicitada; aguardando a próxima leitura da aba." in html
    assert "Retomar verificação</button>" not in html and "Coleta pausada" not in html


def test_after_resume_ok_ingestion_activates_with_new_data(env):
    _block_after_ok(env)
    env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    env.now = NOW + 5 * MIN
    body = env.post(page_html(alfa="enfrenta problemas")).get_json()
    assert body["estado"] == "ativo"
    html = env.home()
    alfa = next(c for c in cards(html) if ">Banco Alfa</a>" in c)
    assert "state-critical" in alfa and "desatualizada" not in html and "Coleta pausada" not in html


def test_after_resume_still_blocked_returns_to_paused(env):
    _block_after_ok(env)
    env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    env.now = NOW + 5 * MIN
    assert env.post(blocked_html()).get_json()["estado"] == "pausado"
    assert env.client.get(ESTADO, headers=HEADERS).get_json()["estado"] == "pausado"
    assert "Coleta pausada: bloqueio detectado às 12:35." in env.home()


def test_resume_when_not_paused_changes_nothing(env):
    _block_after_ok(env)
    csrf = env.csrf()  # o campo oculto so aparece com a coleta pausada
    env.client.post("/coleta/retomar", data={"csrf": csrf})
    env.post(page_html())  # volta a ativo
    assert env.client.post("/coleta/retomar", data={"csrf": csrf}).status_code == 303
    assert env.client.get(ESTADO, headers=HEADERS).get_json()["estado"] == "ativo"


# --- rotas so existem no modo navegador -----------------------------------------------------------------


def test_api_routes_do_not_exist_outside_browser_mode(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)  # demo
    client = create_app(cfg, clock=lambda: NOW).test_client()
    assert client.post("/api/coleta", json={"url": URL, "html": "x"}, headers=HEADERS).status_code == 404
    assert client.get(ESTADO, headers=HEADERS).status_code == 404
    assert client.post("/coleta/retomar", data={"csrf": "x"}).status_code == 404
    assert client.get("/").status_code == 200
    assert "Retomar verificação" not in client.get("/").get_data(as_text=True)


def test_enabling_api_requires_a_token(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="navegador")
    with pytest.raises(ValueError, match="token"):
        create_app(cfg, coleta_api=True)
    with pytest.raises(ValueError, match="navegador"):
        create_app(make_cfg(tmp_path, monkeypatch), coleta_api=True, coleta_token=TOKEN)


# --- o token nunca vaza -----------------------------------------------------------------------------------


def test_token_never_appears_in_responses_or_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [
        env.post(page_html()),
        env.post(page_html(), token="errado-" + TOKEN),
        env.client.get(ESTADO, headers=HEADERS),
        env.client.get("/"),
        env.post(blocked_html()),
    ]
    for resp in responses:
        assert TOKEN.encode() not in resp.get_data() and TOKEN not in str(resp.headers)
    assert TOKEN not in caplog.text and "errado-" not in caplog.text


def test_dashboard_without_data_in_browser_mode_still_renders(env):
    html = env.home()
    assert BANNER in html and "Sem dados" in html


def test_external_origin_banner_is_not_simulation(tmp_path, monkeypatch):
    cfg = dataclasses.replace(
        make_cfg(tmp_path, monkeypatch), collector="navegador",
        browser_origins=("https://exemplo.test",), browser_authorization_ref="contrato 1")
    client = create_app(cfg, clock=lambda: NOW, coleta_api=True, coleta_token=TOKEN).test_client()
    html = client.get("/").get_data(as_text=True)
    assert "COLETA PELO NAVEGADOR" in html and "SIMULAÇÃO LOCAL" not in html
    rid = client.post("/api/coleta/reserva", json={"alvo": "https://exemplo.test/x"}, headers=HEADERS).get_json()
    assert client.post("/api/coleta", json={"url": "https://exemplo.test/x", "html": "<p></p>",
                                            "reserva_id": rid["reserva_id"]},
                       headers=HEADERS).status_code == 200
