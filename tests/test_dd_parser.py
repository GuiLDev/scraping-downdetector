"""Parser do HTML do Downdetector. O fragmento do Claro e o exemplo real enviado pelo usuario;
os demais servicos sao SINTETICOS (nomes ficticios) para cobrir casos de borda."""

import json

import pytest

from bankmon.collectors import dd_parser
from bankmon.collectors.dd_parser import parse_html

CLARO_REAL = """<div role="img"
  aria-label="Relatos de Claro nas últimas 24 horas. Status atual: enfrenta problemas">
  <svg><path stroke="var(--color-dd-red)"></path></svg></div>"""


def chart(name, status, stroke='stroke="var(--color-dd-blue)"', extra=""):
    label = f"Relatos de {name} nas últimas 24 horas. Status atual: {status}"
    return f'<div class="c"><div role="img" aria-label="{label}"><svg>{extra}<path {stroke}></path></svg></div></div>'


def test_real_fragment_from_user():
    r = parse_html(CLARO_REAL)
    assert r["page_state"] == "ok" and r["total"] == 1
    assert r["services"][0] == {
        "service": "Claro", "status_original": "enfrenta problemas", "status": "critical", "color": "red",
        "stroke": "var(--color-dd-red)", "source": "dom", "consistent": True, "issue": None, "duplicate": False,
    }


@pytest.mark.parametrize("status,stroke,esperado,cor", [
    ("sem problemas", "var(--color-dd-blue)", "normal", "blue"),
    ("possíveis problemas", "var(--color-dd-yellow)", "warning", "yellow"),
    ("enfrenta problemas", "var(--color-dd-red)", "critical", "red"),
])
def test_text_and_color_mapping(status, stroke, esperado, cor):
    s = parse_html(chart("Servico Sintetico", status, f'stroke="{stroke}"'))["services"][0]
    assert (s["status"], s["color"], s["consistent"]) == (esperado, cor, True)


@pytest.mark.parametrize("status,stroke,issue", [
    ("sem problemas", 'stroke="var(--color-dd-red)"', "texto_e_cor_divergem"),
    ("enfrenta problemas", "", "cor_ausente"),
    ("sem problemas", 'stroke="#123456"', "cor_desconhecida"),
    ("em manutenção", 'stroke="var(--color-dd-blue)"', "estado_textual_desconhecido"),
])
def test_inconsistent_is_unknown_never_normal(status, stroke, issue):
    s = parse_html(chart("Servico Sintetico", status, stroke))["services"][0]
    assert s["status"] == "unknown" and not s["consistent"] and s["issue"] == issue


def test_stroke_in_style_attribute_and_first_path_wins():
    html = chart("Servico Sintetico", "possíveis problemas", 'style="stroke: var(--color-dd-yellow)"',
                 extra="") + chart("Outro Sintetico", "sem problemas",
                                   extra='<path stroke="var(--color-dd-blue)"></path>',
                                   stroke='stroke="var(--color-dd-red)"')
    a, b = parse_html(html)["services"]
    assert a["status"] == "warning" and a["consistent"]
    assert b["stroke"] == "var(--color-dd-blue)" and b["status"] == "normal"


def test_name_missing_and_unrelated_images_ignored():
    html = ('<div role="img" aria-label="Logotipo"><svg><path stroke="var(--color-dd-red)"/></svg></div>'
            '<div role="img" aria-label="Status atual: sem problemas"><svg><path stroke="var(--color-dd-blue)"/></svg></div>')
    r = parse_html(html)
    assert r["total"] == 1 and r["services"][0]["issue"] == "nome_ausente" and r["services"][0]["status"] == "unknown"


def test_duplicates_flagged_and_summary():
    html = (chart("Servico A", "sem problemas") + chart("Servico A", "sem problemas")
            + chart("Servico B", "enfrenta problemas", 'stroke="var(--color-dd-red)"'))
    r = parse_html(html)
    assert r["duplicates"] == ["Servico A"] and r["total"] == 3
    assert r["summary"] == {"normal": 2, "warning": 0, "critical": 1, "unknown": 0}


def test_cloudflare_page_is_blocked_and_yields_no_services():
    html = '<html><title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/x.js"></script>' + CLARO_REAL
    r = parse_html(html)
    assert r["page_state"] == "blocked_by_cloudflare" and r["services"] == [] and r["blocked_markers"]


def test_cloudflare_as_legit_service_is_not_a_block():
    # Pagina normal: "Cloudflare" e um servico listado e o script de JavaScript Detections esta presente.
    html = ('<html><head><title>Downdetector</title>'
            '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head><body>'
            + chart("Cloudflare", "sem problemas") + CLARO_REAL + '<footer>Cloudflare</footer></body></html>')
    r = parse_html(html)
    assert r["page_state"] == "ok" and r["blocked_markers"] == [] and r["total"] == 2
    assert {s["service"]: s["status"] for s in r["services"]} == {"Cloudflare": "normal", "Claro": "critical"}


@pytest.mark.parametrize("html", [
    '<html><head><title>Um momento…</title></head><body><p>Verificando se você é humano. '
    'Isso pode levar alguns segundos.</p></body></html>',
    '<html><head><title>Attention Required! | Cloudflare</title></head><body><div id="cf-error-details">'
    '<h1>Sorry, you have been blocked</h1><span>Error 1020</span></div></body></html>',
    '<html><body><script>window._cf_chl_opt={cType:"managed"};</script><form id="challenge-form"></form></body></html>',
])
def test_real_challenge_or_block_pages_are_blocked(html):
    r = parse_html(html + CLARO_REAL)
    assert r["page_state"] == "blocked_by_cloudflare" and r["services"] == []


def test_empty_or_truncated_page_is_incomplete():
    assert parse_html("<html><body>carregando</body></html>")["page_state"] == "incomplete"
    r = parse_html(chart("Servico A", "sem problemas")[:-12])  # HTML truncado no meio
    assert r["total"] == 1 and r["services"][0]["status"] == "normal"


def test_no_redos_on_title_or_aria_label():
    import time

    t = time.perf_counter()
    parse_html("<title" * 40000)  # sem '>': antes era O(n^2)
    label = "Relatos de " + " " * 40000 + "x"
    parse_html(f'<div role="img" aria-label="{label} Status atual: sem problemas"><svg><path/></svg></div>')
    assert time.perf_counter() - t < 0.5


def test_cli_rejects_files_over_limit(tmp_path, capsys):
    f = tmp_path / "grande.html"
    f.write_bytes(b" " * (dd_parser.MAX_BYTES + 1))
    assert dd_parser.main([str(f)]) == 2
    assert "recusado" in capsys.readouterr().err


def test_parser_module_has_no_network_imports():
    import ast, inspect
    tree = ast.parse(inspect.getsource(dd_parser))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"socket", "http", "urllib", "requests", "httpx", "aiohttp"}


def test_cli_outputs_json(tmp_path, capsys):
    f = tmp_path / "pagina.html"
    f.write_text(CLARO_REAL, encoding="utf-8")
    assert dd_parser.main([str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["services"][0]["service"] == "Claro"
