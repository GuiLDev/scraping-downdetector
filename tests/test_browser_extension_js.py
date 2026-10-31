"""Logica do background.js da extensao, executada em Node com um `chrome` falso (sem navegador).

Os cenarios cobrem o contrato: so consulta/le/recarrega a aba SEM ativa-la, respeita a pausa, espera
a pagina estabilizar (ate 45 s) e envia o HTML ao servidor com o token no cabecalho.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from bankmon.config import PROJECT_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js nao encontrado")
SCRIPT = PROJECT_ROOT / "extension" / "coletor" / "background.js"
HARNESS = PROJECT_ROOT / "tests" / "js" / "background_harness.js"


def reading(graficos=12, html="<html>pagina</html>", desafio=False):
    return {"html": html, "graficos": graficos, "desafio": desafio}


def run_scenario(**scenario):
    out = subprocess.run([NODE, str(HARNESS), str(SCRIPT)], input=json.dumps(scenario),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_collects_reloads_target_tab_without_activating_and_posts_html():
    rep = run_scenario(readings=[reading(5), reading(12), reading(12, "<html>final</html>")])
    assert "error" not in rep
    assert rep["reloads"] == [1] and rep["forbidden"] == []
    assert rep["estadoToken"] == "TOKEN-DE-TESTE"
    (post,) = rep["posts"]
    assert post["headers"]["X-BankMonitor-Token"] == "TOKEN-DE-TESTE"
    assert post["body"] == {"url": "http://127.0.0.1:8777/dd?x=1", "html": "<html>final</html>",
                            "reserva_id": "RES-1"}
    assert rep["result"]["resultado"] == "enviado"


def test_does_nothing_while_collection_is_paused():
    rep = run_scenario(estado="pausado", readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reads"] == 0
    assert rep["result"]["resultado"] == "pausado"


def test_reserves_the_slot_before_reloading_and_sends_the_reservation_id():
    rep = run_scenario(readings=[reading(12), reading(12)])
    assert rep["order"] == ["estado", "reserva", "reload", "post"]
    (reserva,) = rep["reservas"]
    assert reserva["body"] == {"alvo": "http://127.0.0.1:8777/dd"}
    assert reserva["headers"]["X-BankMonitor-Token"] == "TOKEN-DE-TESTE"


@pytest.mark.parametrize("status,body,result", [
    (429, {"erro": "intervalo"}, "intervalo"),
    (409, {"erro": "reserva_ativa"}, "reserva_ativa"),
    (409, {"erro": "pausado"}, "pausado"),
])
def test_refused_reservation_never_reloads_nor_posts(status, body, result):
    rep = run_scenario(reservaStatus=status, reservaBody=body, readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reads"] == 0
    assert rep["result"]["resultado"] == result and rep["cleared"] == []


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_reservation_stops_everything_and_clears_the_alarm(status):
    rep = run_scenario(reservaStatus=status, readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["cleared"] == ["bankmon-coleta"]
    assert rep["result"]["resultado"] == "nao_autorizado"


def test_server_error_on_reservation_does_not_reload():
    rep = run_scenario(reservaStatus=500, readings=[reading()])
    assert rep["reloads"] == [] and rep["result"]["resultado"] == "erro"


def test_verifying_state_reads_the_current_dom_without_reload_or_reservation():
    rep = run_scenario(estado="verificando", readings=[reading(12), reading(12, "<html>atual</html>")])
    assert rep["reloads"] == [] and rep["reservas"] == [] and rep["forbidden"] == []
    assert rep["order"] == ["estado", "post"] and rep["reads"] >= 1
    (post,) = rep["posts"]
    assert post["body"]["modo"] == "verificacao" and "reserva_id" not in post["body"]
    assert post["body"]["url"] == "http://127.0.0.1:8777/dd?x=1"
    assert rep["result"]["resultado"] == "enviado"


def test_verifying_state_sends_a_challenge_page_as_is_and_still_never_reloads():
    rep = run_scenario(estado="verificando", readings=[reading(0, "<title>Just a moment...</title>", desafio=True)])
    assert rep["reloads"] == [] and rep["posts"][0]["body"]["modo"] == "verificacao"


def test_verifying_without_the_tab_does_nothing():
    rep = run_scenario(estado="verificando", tabs=[{"id": 9, "url": "http://127.0.0.1:1/x", "active": True}],
                       readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["result"]["resultado"] == "sem_aba"


def test_without_matching_tab_it_neither_reserves_reloads_nor_posts():
    rep = run_scenario(tabs=[{"id": 9, "url": "http://127.0.0.1:8777/outro", "active": True}], readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reservas"] == [] and rep["result"]["resultado"] == "sem_aba"


def test_challenge_signal_posts_immediately_without_waiting_for_stability():
    rep = run_scenario(readings=[reading(0, "<title>Just a moment...</title>", desafio=True)])
    assert rep["reads"] == 1 and len(rep["posts"]) == 1
    assert "Just a moment" in rep["posts"][0]["body"]["html"]


def test_waits_until_chart_count_is_stable_in_two_reads():
    rep = run_scenario(readings=[reading(0), reading(3), reading(7), reading(7)])
    assert rep["reads"] == 4 and len(rep["posts"]) == 1


def test_gives_up_waiting_after_45_seconds_and_still_posts_the_last_reading():
    changing = [reading(n, f"<p>{n}</p>") for n in range(1, 200)]
    rep = run_scenario(readings=changing)
    assert 45_000 <= rep["elapsedMs"] <= 60_000
    assert len(rep["posts"]) == 1


def test_page_that_never_completes_loading_is_abandoned_without_posting():
    rep = run_scenario(neverComplete=True, readings=[reading()])
    assert rep["posts"] == [] and rep["result"]["resultado"] == "carregamento_incompleto"


def test_html_over_5mb_is_not_sent():
    rep = run_scenario(readings=[reading(12, "x" * (5 * 1024 * 1024 + 1))])
    assert rep["posts"] == [] and rep["result"]["resultado"] == "html_grande_demais"


def test_alarm_period_is_the_configured_interval_with_half_minute_floor():
    assert run_scenario(mode="alarm", config={"intervalo_min": 7})["alarms"][0]["periodInMinutes"] == 7
    assert run_scenario(mode="alarm", config={"intervalo_min": 0.1})["alarms"][0]["periodInMinutes"] == 0.5
    assert run_scenario(mode="alarm")["hasAlarmHandler"] is True


# --- revisao de seguranca: SEC-002, SEC-004, SEC-005 -------------------------------------------------


def test_state_request_carries_the_target_so_the_server_can_revoke_it():
    rep = run_scenario(readings=[reading(12), reading(12)])
    assert rep["estadoUrls"] == ["http://127.0.0.1:8765/api/coleta/estado?alvo=http%3A%2F%2F127.0.0.1%3A8777%2Fdd"]


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_state_stops_everything_and_clears_the_alarm(status):
    rep = run_scenario(estadoStatus=status, readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reads"] == 0
    assert rep["cleared"] == ["bankmon-coleta"] and rep["result"]["resultado"] == "nao_autorizado"


def test_other_state_errors_do_not_reload_nor_clear_the_alarm():
    rep = run_scenario(estadoStatus=500, readings=[reading()])
    assert rep["reloads"] == [] and rep["cleared"] == [] and rep["result"]["resultado"] == "erro"


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_post_clears_the_alarm(status):
    rep = run_scenario(postStatus=status, readings=[reading(12), reading(12)])
    assert rep["cleared"] == ["bankmon-coleta"] and rep["result"]["resultado"] == "nao_autorizado"


def test_tab_match_uses_origin_and_path_prefix_not_raw_startswith():
    tabs = [
        {"id": 1, "url": "http://localhost.evil.example/", "active": False},
        {"id": 2, "url": "http://localhost:3000/", "active": False},
        {"id": 3, "url": "http://localhost/pagina?x=1", "active": False},
    ]
    perms = ["http://localhost/*", "http://localhost.evil.example/*", "http://localhost:3000/*", "http://127.0.0.1:8765/*"]
    rep = run_scenario(tabs=tabs, hostPermissions=perms, config={"alvo": "http://localhost/"},
                       readings=[reading(12), reading(12)])
    assert rep["reloads"] == [3]


def test_path_prefix_matches_the_exact_page_and_subpaths_but_not_siblings():
    base = {"active": False}
    tabs = [{"id": 1, "url": "http://127.0.0.1:8777/ddx", **base}, {"id": 2, "url": "http://127.0.0.1:8777/outro", **base},
            {"id": 3, "url": "http://127.0.0.1:8777/dd", **base}]
    rep = run_scenario(tabs=tabs, config={"alvo": "http://127.0.0.1:8777/dd/"}, readings=[reading(12), reading(12)])
    assert rep["reloads"] == [3]
    rep = run_scenario(tabs=[{"id": 4, "url": "http://127.0.0.1:8777/dd/sub?q=1", **base}],
                       config={"alvo": "http://127.0.0.1:8777/dd/"}, readings=[reading(12), reading(12)])
    assert rep["reloads"] == [4]


def test_works_without_the_tabs_permission_using_only_host_permissions():
    rep = run_scenario(readings=[reading(12), reading(12)])  # o harness esconde `url` fora dos host_permissions
    assert rep["forbidden"] == [] and rep["reloads"] == [1] and set(rep["scripted"]) == {1} and len(rep["posts"]) == 1


# --- leitura pontual (config.json com `pontual: true`) -----------------------------------------------


def test_pontual_never_creates_an_alarm_and_clears_an_existing_one():
    rep = run_scenario(mode="alarm", config={"pontual": True})
    assert rep["alarms"] == [] and rep["cleared"] == ["bankmon-coleta"]


def test_pontual_reserves_but_never_reloads_and_posts_the_current_dom():
    rep = run_scenario(config={"pontual": True}, readings=[reading(12, "<html>atual</html>"), reading(12, "<html>atual</html>")])
    assert rep["reloads"] == [] and rep["forbidden"] == []
    assert rep["order"] == ["estado", "reserva", "post"]
    (post,) = rep["posts"]
    assert post["body"] == {"url": "http://127.0.0.1:8777/dd?x=1", "html": "<html>atual</html>",
                            "reserva_id": "RES-1", "modo": "pontual"}
    assert rep["result"]["resultado"] == "enviado"


def test_pontual_refused_reservation_reads_and_posts_nothing():
    rep = run_scenario(config={"pontual": True}, reservaStatus=429, readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reads"] == 0
    assert rep["result"]["resultado"] == "intervalo"


def test_pontual_paused_state_does_nothing():
    rep = run_scenario(config={"pontual": True}, estado="pausado", readings=[reading()])
    assert rep["reservas"] == [] and rep["posts"] == [] and rep["reads"] == 0


def test_without_the_pontual_flag_the_alarm_is_still_created():
    assert len(run_scenario(mode="alarm", config={"pontual": False})["alarms"]) == 1


def test_pontual_reservation_carries_modo_pontual_and_normal_one_does_not():
    rep = run_scenario(config={"pontual": True}, readings=[reading(12), reading(12)])
    assert rep["reservas"][0]["body"] == {"alvo": "http://127.0.0.1:8777/dd", "modo": "pontual"}
    rep = run_scenario(readings=[reading(12), reading(12)])
    assert rep["reservas"][0]["body"] == {"alvo": "http://127.0.0.1:8777/dd"}


def test_normal_extension_with_a_409_reservation_never_reloads():
    rep = run_scenario(reservaStatus=409, reservaBody={"erro": "pontual"}, readings=[reading()])
    assert rep["reloads"] == [] and rep["posts"] == [] and rep["reads"] == 0


def test_pontual_ignores_the_verifying_state_and_follows_the_reservation_path():
    rep = run_scenario(config={"pontual": True}, estado="verificando", readings=[reading(12), reading(12)])
    assert rep["reloads"] == [] and rep["order"] == ["estado", "reserva", "post"]
    assert rep["posts"][0]["body"]["modo"] == "pontual" and rep["posts"][0]["body"]["reserva_id"] == "RES-1"


def test_a_leftover_alarm_with_pontual_config_clears_itself_and_does_nothing():
    rep = run_scenario(mode="fire_alarm", config={"pontual": True}, readings=[reading()])
    assert rep["reads"] == 0 and rep["reservas"] == [] and rep["posts"] == [] and rep["estadoGets"] == 0
    assert rep["cleared"].count("bankmon-coleta") >= 2 and rep["reloads"] == []


def test_a_normal_alarm_still_collects():
    rep = run_scenario(mode="fire_alarm", readings=[reading(12), reading(12)])
    assert len(rep["posts"]) == 1 and rep["reloads"] == [1]


# --- clique no icone (extensao pontual real: sem `chrome.alarms`, com `chrome.action`) ----------------

PONTUAL_REAL = {"noAlarms": True, "withAction": True, "config": {"pontual": True}}


def click(**scenario):
    return run_scenario(mode="click", **{**PONTUAL_REAL, **scenario})


def test_click_runs_one_pontual_reading_without_reload_and_shows_ok():
    rep = click(readings=[reading(12, "<html>atual</html>"), reading(12, "<html>atual</html>")])
    assert "error" not in rep and rep["clickListeners"] == 1 and rep["alarms"] == []
    assert rep["reloads"] == [] and rep["forbidden"] == []
    assert rep["order"] == ["estado", "reserva", "post"]
    assert rep["reservas"][0]["body"] == {"alvo": "http://127.0.0.1:8777/dd", "modo": "pontual"}
    (post,) = rep["posts"]
    assert post["body"]["modo"] == "pontual" and post["body"]["reserva_id"] == "RES-1"
    assert rep["badges"] == ["...", "OK"]
    assert "TOKEN-DE-TESTE" not in json.dumps(rep["titles"]) and "<html>" not in json.dumps(rep["titles"])
    assert "enviado (200)" in rep["titles"][-1]


def test_click_with_a_refused_interval_shows_60m_and_reads_nothing():
    rep = click(reservaStatus=429, readings=[reading()])
    assert rep["badges"][-1] == "60m" and rep["reads"] == 0 and rep["posts"] == [] and rep["reloads"] == []


def test_click_without_the_tab_shows_aba():
    rep = click(tabs=[{"id": 9, "url": "http://127.0.0.1:1/x", "active": True}], readings=[reading()])
    assert rep["badges"][-1] == "ABA" and rep["posts"] == [] and rep["reservas"] == []


@pytest.mark.parametrize("scenario", [{"postStatus": 500}, {"reservaStatus": 500}, {"estadoStatus": 500}])
def test_click_shows_err_for_other_outcomes(scenario):
    rep = click(readings=[reading(12), reading(12)], **scenario)
    assert rep["badges"][-1] == "ERR"


def test_click_on_unauthorized_shows_err_without_throwing():
    rep = click(reservaStatus=401, readings=[reading()])
    assert "error" not in rep and rep["badges"][-1] == "ERR" and rep["posts"] == []


def test_automatic_extension_registers_no_click_listener_and_keeps_the_alarm():
    rep = run_scenario(mode="click", readings=[reading()])  # sem `chrome.action`
    assert rep["hasClickHandler"] is False and rep["reservas"] == [] and rep["posts"] == []
    assert len(run_scenario(mode="alarm", withAction=False)["alarms"]) == 1


def test_click_is_ignored_when_the_config_is_not_pontual():
    rep = run_scenario(mode="click", withAction=True, readings=[reading(12), reading(12)])
    assert rep["clickListeners"] == 1
    assert rep["estadoGets"] == 0 and rep["reservas"] == [] and rep["posts"] == [] and rep["reloads"] == []
    assert rep["badges"] == []


def test_pontual_without_chrome_alarms_loads_and_collects_and_handles_401():
    rep = run_scenario(noAlarms=True, config={"pontual": True}, readings=[reading(12), reading(12)])
    assert "error" not in rep and rep["result"]["resultado"] == "enviado" and rep["reloads"] == []
    rep = run_scenario(noAlarms=True, config={"pontual": True}, reservaStatus=401, readings=[reading()])
    assert "error" not in rep and rep["result"]["resultado"] == "nao_autorizado"
