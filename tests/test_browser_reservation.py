"""SEC-014: reserva explicita da vaga ANTES da recarga + retomada sem recarga (TDD)."""

from __future__ import annotations

import dataclasses
import threading
from datetime import timedelta

import pytest

from bankmon import db
from bankmon.web import create_app
from browser_helpers import ESTADO, URL, blocked_html, page_html
from test_browser_api import HEADERS, TOKEN, Env
from webutil import MIN, NOW

EXTERNAL = "https://exemplo.test/status"
SEC = timedelta(seconds=1)
VERIFY = "verificacao"


def rebuild(e, **changes):
    e.cfg = dataclasses.replace(e.cfg, **changes)
    e.app = create_app(e.cfg, clock=lambda: e.now, coleta_api=True, coleta_token=TOKEN)
    e.client = e.app.test_client()
    return e


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def slow(env):
    """Mesmo ambiente com intervalo minimo local de 30 s."""
    return rebuild(env, browser_local_interval_s=30)


@pytest.fixture
def ext(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    return rebuild(e, browser_origins=("https://exemplo.test",), browser_authorization_ref="X")


def reserve_rows(e):
    return e.rows("SELECT origin, reserved_at, expires_at, consumed_at IS NOT NULL FROM collector_reservations")


def estado(e):
    return e.client.get(ESTADO, headers=HEADERS).get_json()


def verify(e, html, url=URL):
    return e.client.post("/api/coleta", json={"url": url, "html": html, "modo": VERIFY}, headers=HEADERS)


# --- POST /api/coleta/reserva ---------------------------------------------------------------------


def test_reservation_is_granted_with_random_id_and_two_minute_validity(env):
    resp = env.reserve()
    body = resp.get_json()
    assert resp.status_code == 200 and set(body) == {"reserva_id", "expira_em"}
    assert len(body["reserva_id"]) >= 20 and body["expira_em"] == "2026-10-28T15:32:00Z"
    assert reserve_rows(env) == [("http://127.0.0.1:8777", "2026-10-28T15:30:00Z", "2026-10-28T15:32:00Z", 0)]


@pytest.mark.parametrize("token", [None, "errado"])
def test_reservation_requires_token(env, token):
    headers = {"X-BankMonitor-Token": token} if token else {}
    assert env.client.post("/api/coleta/reserva", json={"alvo": URL}, headers=headers).status_code == 401
    assert not env.cfg.db_path.exists()


def test_reservation_validates_body_and_origin(env):
    def post(**kw):
        return env.client.post("/api/coleta/reserva", headers=HEADERS, **kw).status_code

    assert post(data=b"nao e json", content_type="application/json") == 400
    assert post(json={"sem": "alvo"}) == 400
    assert post(json={"alvo": 5}) == 400
    assert post(json={"alvo": "http://evil.test:8777/dd/"}) == 403
    assert post(json={"alvo": "http://127.0.0.1:9999/dd/"}) == 403
    assert not env.cfg.db_path.exists()


def test_second_reservation_while_one_is_open_is_409_reserva_ativa(env):
    assert env.reserve().status_code == 200
    resp = env.reserve()
    assert resp.status_code == 409 and resp.get_json()["erro"] == "reserva_ativa"
    assert len(reserve_rows(env)) == 1


def test_reservation_while_paused_is_409_pausado_and_creates_nothing(env):
    env.post(blocked_html())
    before = len(reserve_rows(env))
    resp = env.reserve()
    assert resp.status_code == 409 and resp.get_json()["erro"] == "pausado"
    assert len(reserve_rows(env)) == before


def test_interval_is_enforced_before_the_reload_with_retry_after(slow):
    assert slow.post(page_html()).status_code == 200
    slow.now = NOW + 10 * SEC
    resp = slow.reserve()
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "20"
    slow.now = NOW + 30 * SEC
    assert slow.reserve().status_code == 200


def test_external_origin_has_a_fixed_60_minute_interval(ext):
    assert ext.post(page_html(), url=EXTERNAL).status_code == 200
    ext.now = NOW + 59 * MIN
    resp = ext.reserve(EXTERNAL)
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "60"
    assert ext.post(page_html(), url=EXTERNAL).status_code == 429
    assert ext.statuses() == ["ok"]  # a leitura recusada nao gravou
    ext.now = NOW + 60 * MIN
    assert ext.reserve(EXTERNAL).status_code == 200


def test_expired_reservation_frees_the_origin_but_the_interval_still_applies(env):
    rebuild(env, browser_local_interval_s=300)
    assert env.reserve().status_code == 200  # nunca consumida (a aba nao respondeu)
    env.now = NOW + 3 * MIN  # expirou (2 min): nao e "reserva_ativa", mas faltam 2 min de intervalo
    resp = env.reserve()
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "120"
    env.now = NOW + 5 * MIN
    assert env.reserve().status_code == 200


def test_limit_survives_a_restart_because_it_lives_in_the_database(slow):
    assert slow.post(page_html()).status_code == 200
    restarted = create_app(slow.cfg, clock=lambda: NOW + 5 * SEC, coleta_api=True, coleta_token=TOKEN)
    resp = restarted.test_client().post("/api/coleta/reserva", json={"alvo": URL}, headers=HEADERS)
    assert resp.status_code == 429


def test_only_one_of_two_simultaneous_reservations_wins(env):
    barrier = threading.Barrier(2)
    codes = []

    def go():
        client = env.app.test_client()
        barrier.wait(10)
        codes.append(client.post("/api/coleta/reserva", json={"alvo": URL}, headers=HEADERS).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(codes) == 2 and codes.count(200) == 1
    assert all(c in (200, 409, 429, 503) for c in codes)
    assert len(reserve_rows(env)) == 1


# --- POST /api/coleta exige e consome a reserva --------------------------------------------------------


def test_post_without_a_valid_reservation_is_429_with_a_single_message_and_writes_nothing(env):
    missing = env.post(page_html(), reserve=False)
    unknown = env.post(page_html(), reserve=False, reserva_id="inexistente")
    assert missing.status_code == unknown.status_code == 429
    assert missing.get_json()["erro"] == unknown.get_json()["erro"]
    assert env.statuses() == []


def test_reservation_is_consumed_once(env):
    rid = env.reserve().get_json()["reserva_id"]
    assert env.post(page_html(), reserva_id=rid).status_code == 200
    assert reserve_rows(env)[0][3] == 1
    assert env.post(page_html(), reserva_id=rid).status_code == 429
    assert env.statuses() == ["ok"]


def test_expired_reservation_is_refused(env):
    rid = env.reserve().get_json()["reserva_id"]
    env.now = NOW + 3 * MIN
    assert env.post(page_html(), reserva_id=rid).status_code == 429
    assert env.statuses() == []


def test_reservation_of_another_origin_is_refused(env):
    rebuild(env, browser_origins=("http://127.0.0.1:8777", "http://localhost:9100"))
    rid = env.reserve("http://localhost:9100/dd").get_json()["reserva_id"]
    assert env.post(page_html(), reserva_id=rid).status_code == 429
    assert env.post(page_html(), url="http://localhost:9100/dd", reserva_id=rid).status_code == 200


def test_consumption_is_atomic_with_the_recording(env, monkeypatch):
    rid = env.reserve().get_json()["reserva_id"]
    original = db.insert_run_services

    def boom(*a, **k):
        raise RuntimeError("falha no meio da gravacao")

    monkeypatch.setattr(db, "insert_run_services", boom)
    assert env.post(page_html(), reserva_id=rid).status_code == 500
    assert env.statuses() == [] and reserve_rows(env)[0][3] == 0  # nada gravado, reserva intacta
    monkeypatch.setattr(db, "insert_run_services", original)
    assert env.post(page_html(), reserva_id=rid).status_code == 200


def test_non_string_reservation_id_or_unknown_mode_is_400(env):
    for extra in ({"reserva_id": 5}, {"modo": "outro"}, {"modo": 3}):
        resp = env.client.post("/api/coleta", json={"url": URL, "html": page_html(), **extra}, headers=HEADERS)
        assert resp.status_code == 400, extra


# --- GET /api/coleta/estado so consulta -------------------------------------------------------------------


def test_state_reports_reservation_availability_without_writing(slow):
    assert slow.client.get(ESTADO, headers=HEADERS).status_code == 200
    assert not slow.cfg.db_path.exists()  # consultar nao cria banco
    slow.post(page_html())
    rows = reserve_rows(slow)
    slow.now = NOW + 10 * SEC
    body = estado(slow)
    assert body["pode_reservar"] is False and body["proxima_leitura_em"] == "2026-10-28T15:30:30Z"
    slow.now = NOW + 40 * SEC
    body = estado(slow)
    assert body["pode_reservar"] is True and body["proxima_leitura_em"] is None
    assert reserve_rows(slow) == rows and slow.statuses() == ["ok"]


def test_state_says_cannot_reserve_when_reservation_open_or_paused(env):
    env.reserve()
    assert estado(env)["pode_reservar"] is False
    env.now = NOW + 3 * MIN
    assert estado(env)["pode_reservar"] is True
    env.post(blocked_html())
    assert estado(env)["pode_reservar"] is False and estado(env)["estado"] == "pausado"


# --- retomada sem recarga ----------------------------------------------------------------------------------


def _paused_then_resumed(env):
    env.post(blocked_html())
    env.client.post("/coleta/retomar", data={"csrf": env.csrf()})
    assert estado(env)["estado"] == "verificando"


def test_resume_only_sets_verifying_and_neither_reserves_nor_reads(env):
    env.post(blocked_html())
    runs, reservations = env.statuses(), reserve_rows(env)
    _paused_then_resumed(env)
    assert env.statuses() == runs and reserve_rows(env) == reservations


def test_verification_mode_reads_the_current_dom_without_reservation(env):
    _paused_then_resumed(env)
    reservations = reserve_rows(env)
    resp = verify(env, page_html())
    assert resp.status_code == 200 and resp.get_json()["estado"] == "ativo"
    assert reserve_rows(env) == reservations and env.statuses()[-1] == "ok"


def test_verification_mode_still_blocked_goes_back_to_paused(env):
    _paused_then_resumed(env)
    resp = verify(env, blocked_html())
    assert resp.status_code == 200 and resp.get_json()["estado"] == "pausado"


@pytest.mark.parametrize("prepare", ["ativo", "pausado"])
def test_verification_mode_is_refused_outside_the_verifying_state(env, prepare):
    env.post(blocked_html() if prepare == "pausado" else page_html())
    runs = env.statuses()
    assert verify(env, page_html()).status_code == 409 and env.statuses() == runs


def test_verification_mode_is_refused_for_external_origin_even_when_verifying(ext):
    ext.post(page_html(), url=EXTERNAL)
    conn = db.open_db(ext.cfg)
    db.set_collector_state(conn, "verificando", "x", NOW)
    conn.close()
    runs = ext.statuses()
    assert verify(ext, page_html(), EXTERNAL).status_code == 409 and ext.statuses() == runs


def test_the_next_reload_after_resume_still_respects_the_interval(slow):
    assert slow.post(page_html()).status_code == 200  # ultima recarga em NOW
    slow.now = NOW + 5 * SEC
    slow.post(blocked_html(), reserve=False)  # pausa por fora do fluxo de reserva
    assert estado(slow)["estado"] == "ativo"  # sem reserva a leitura e recusada (429)
    # forca a pausa como a extensao faria depois de uma recarga reservada
    slow.now = NOW + 30 * SEC
    slow.post(blocked_html())
    slow.client.post("/coleta/retomar", data={"csrf": slow.csrf()})
    slow.now = NOW + 40 * SEC
    assert verify(slow, page_html()).get_json()["estado"] == "ativo"
    assert slow.reserve().status_code == 429  # 10 s desde a ultima recarga (30 s) < 30 s
    slow.now = NOW + 61 * SEC
    assert slow.reserve().status_code == 200


def test_default_port_and_implicit_port_share_one_reservation_and_interval(ext):
    ext = rebuild(ext, browser_origins=("https://exemplo.test",))
    assert ext.reserve("https://exemplo.test:443/status").status_code == 200
    resp = ext.reserve("https://exemplo.test/status")
    assert resp.status_code == 409 and resp.get_json()["erro"] == "reserva_ativa"
    assert ext.rows("SELECT DISTINCT origin FROM collector_reservations") == [("https://exemplo.test",)]


def test_over_long_url_is_refused_before_consuming_the_reservation(env):
    rid = env.reserve().get_json()["reserva_id"]
    resp = env.post(page_html(), url=URL + "/" + "a" * 500, reserva_id=rid)
    assert resp.status_code == 403
    assert reserve_rows(env)[0][3] == 0
