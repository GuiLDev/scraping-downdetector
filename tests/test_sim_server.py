"""Servidor de simulacao dos testes E2E: conteudo gerado (sem rede) lido pelo parser real."""

from __future__ import annotations

import pytest

from bankmon.collectors.dd_parser import parse_html
from e2e import sim_server

LABELS = ["Banco Alfa", "Banco Beta", "Banco Gama", "Banco Delta"]


def parsed(mode):
    return parse_html(sim_server.render(mode, LABELS), min_services=10)


def by_name(result):
    return {s["service"]: s for s in result["services"]}


def test_normal_a_has_the_real_format_with_configured_institution_names():
    result = parsed("normal_a")
    assert result["page_state"] == "ok"
    services = by_name(result)
    assert set(LABELS) <= set(services) and len(services) >= 10
    first = services["Banco Alfa"]
    assert first["status"] == "warning" and first["color"] == "yellow"
    assert first["svg"]["viewbox"] and first["svg"]["path_d"].startswith("M")
    assert all(services[n]["status"] == "normal" for n in LABELS[1:])


def test_normal_b_differs_from_normal_a_in_states_and_geometry():
    a, b = by_name(parsed("normal_a")), by_name(parsed("normal_b"))
    assert b["Banco Alfa"]["status"] == "normal" and b["Banco Beta"]["status"] == "critical"
    assert a["Banco Beta"]["svg"]["path_d"] != b["Banco Beta"]["svg"]["path_d"]


def test_blocking_mode_is_a_challenge_page():
    html = sim_server.render("bloqueio", LABELS)
    assert "<title>Just a moment...</title>" in html and "cf_chl_opt" in html
    assert parse_html(html, min_services=10)["page_state"] == "blocked_by_cloudflare"


def test_blocking_page_offers_a_human_button_that_only_acts_on_user_click():
    html = sim_server.render("bloqueio", LABELS)
    assert "Sou humano (simulação)" in html and "<button" in html
    assert "addEventListener('click'" in html and "/_sim/resolver" in html and "location.reload()" in html
    script = html.split("<script>", 2)[-1]
    assert script.index("addEventListener('click'") < script.index("location.reload()")


def test_human_resolution_turns_the_challenge_into_the_normal_page_via_post_only():
    state = sim_server.SimState(LABELS, "bloqueio")
    assert sim_server.respond("/_sim/resolver", "", state)[0] == 405  # GET nao resolve
    assert state.mode == "bloqueio"
    assert sim_server.respond("/_sim/resolver", "", state, "POST")[0] == 200
    assert state.mode == "normal_a" and b"Banco Alfa" in sim_server.respond("/dd", "", state)[2]
    state.mode = "normal_b"  # fora do desafio o clique nao muda nada
    sim_server.respond("/_sim/resolver", "", state, "POST")
    assert state.mode == "normal_b"


def test_page_requests_are_counted_and_exposed_but_control_calls_are_not():
    state = sim_server.SimState(LABELS)
    sim_server.respond("/dd", "", state)
    sim_server.respond("/", "", state)
    sim_server.respond("/_sim/modo", "valor=normal_b", state)
    status, ctype, body = sim_server.respond("/_sim/contagem", "", state)
    assert status == 200 and ctype.startswith("application/json") and body == b'{"paginas": 2}'


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        sim_server.render("outro", LABELS)


def test_respond_serves_the_page_and_switches_mode():
    state = sim_server.SimState(LABELS)
    status, ctype, body = sim_server.respond("/dd", "", state)
    assert status == 200 and ctype.startswith("text/html") and b"Banco Alfa" in body
    assert sim_server.respond("/_sim/modo", "valor=bloqueio", state)[0] == 200
    assert b"Just a moment" in sim_server.respond("/", "", state)[2]
    assert sim_server.respond("/_sim/modo", "valor=hack", state)[0] == 400
    assert state.mode == "bloqueio"
    assert sim_server.respond("/qualquer", "", state)[0] == 404


def test_simulator_never_imports_the_real_site_or_network_clients():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(sim_server.__file__).read_text("utf-8"))
    modules = {n.name for a in ast.walk(tree) if isinstance(a, ast.Import) for n in a.names}
    modules |= {a.module for a in ast.walk(tree) if isinstance(a, ast.ImportFrom) and a.module}
    clients = {"requests", "httpx", "aiohttp", "playwright", "selenium", "urllib.request", "http.client"}
    assert not modules & clients
    assert "downdetector.com" not in Path(sim_server.__file__).read_text("utf-8")


def test_e2e_marker_is_registered_and_off_by_default_with_optional_extra():
    import tomllib

    from bankmon.config import PROJECT_ROOT

    cfg = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    pytest_cfg = cfg["tool"]["pytest"]["ini_options"]
    assert "not e2e" in pytest_cfg["addopts"] and any(m.startswith("e2e:") for m in pytest_cfg["markers"])
    assert cfg["project"]["optional-dependencies"]["e2e"] == ["playwright>=1.64"]
