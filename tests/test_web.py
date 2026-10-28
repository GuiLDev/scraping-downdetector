"""Dashboard (Flask test client): secoes, filtros, avisos, estados, seguranca."""

from __future__ import annotations

import html as htmllib
import re
import sqlite3

import pytest

from bankmon import db
from webutil import (
    INSTITUTIONS,
    MIN,
    NOW,
    add_run,
    cards,
    client_for,
    json_block,
    make_cfg,
    page,
)

LEVELS = {"alfa": "critical", "beta": "attention", "gama": "no_alert", "delta": "not_listed"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch)


@pytest.fixture
def seeded(cfg):
    add_run(cfg, NOW - 10 * MIN, LEVELS)
    return cfg


def names_in_order(html: str) -> list[str]:
    return re.findall(r'<h4 class="card-name"><a [^>]*>(.*?)</a>', html)


# --- Secoes e ordenacao -------------------------------------------------------


def test_three_sections_in_order_with_cards(seeded):
    html = page(client_for(seeded))
    pos = [html.index(t) for t in ("Com alerta", "Sem alerta identificado", "Sem cobertura")]
    assert pos == sorted(pos)
    assert names_in_order(html) == ["Banco Alfa", "Banco Beta", "Banco Gama", "Banco Delta"]


def test_critical_before_attention_even_across_categories(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    # beta (categoria 1) em atencao, gama (categoria 2) em critico
    add_run(cfg, NOW - 5 * MIN, {"alfa": "no_alert", "beta": "attention", "gama": "critical", "delta": "no_alert"})
    names = names_in_order(page(client_for(cfg)))
    assert names[:2] == ["Banco Gama", "Banco Beta"]


def test_cards_show_state_icon_text_and_since(seeded):
    html = page(client_for(seeded))
    alfa = next(c for c in cards(html) if "Banco Alfa" in c)
    assert "state-critical" in alfa and "Crítico" in alfa and 'aria-hidden="true"' in alfa
    assert "desde 12:20" in alfa  # transicao gravada em 15:20 UTC = 12:20 local
    for label in ("Atenção", "Sem alerta", "Não monitorado"):
        assert label in html


def test_header_last_and_next_verification(seeded):
    html = page(client_for(seeded))
    assert "28/10 12:20 (há 10 min)" in html
    assert re.search(r"Próxima verificação prevista:</strong> 13:20", html)
    assert "Última verificação válida:" in html


def test_header_next_marks_overdue(seeded):
    from datetime import timedelta

    html = page(client_for(seeded, NOW + timedelta(hours=3)))
    assert "(atrasada)" in html


def test_meta_refresh_and_disclaimer(seeded):
    html = page(client_for(seeded))
    assert '<meta http-equiv="refresh" content="300">' in html
    assert "Indicadores segundo o Downdetector; não são confirmação oficial de indisponibilidade." in html


# --- Filtros ------------------------------------------------------------------


def test_filter_state(seeded):
    names = names_in_order(page(client_for(seeded), "/?state=critical"))
    assert names == ["Banco Alfa"]


def test_filter_category(seeded):
    names = names_in_order(page(client_for(seeded), "/?category=digitais"))
    assert names == ["Banco Gama", "Banco Delta"]


def test_filter_name_query_case_insensitive(seeded):
    assert names_in_order(page(client_for(seeded), "/?q=bETa")) == ["Banco Beta"]


def test_filters_combine(seeded):
    assert names_in_order(page(client_for(seeded), "/?category=digitais&state=no_alert")) == ["Banco Gama"]


@pytest.mark.parametrize(
    "qs",
    [
        "state=bogus",
        "category=nao-existe",
        "category=%27%3B%20DROP%20TABLE%20runs--",
        "state=",
        "q=" + "x" * 500,
    ],
)
def test_invalid_filter_values_never_500(seeded, qs):
    resp = client_for(seeded).get("/?" + qs)
    assert resp.status_code == 200
    if not qs.startswith("q="):
        assert len(names_in_order(resp.get_data(as_text=True))) == 4  # filtro ignorado


def test_empty_filter_result_keeps_three_sections(seeded):
    html = page(client_for(seeded), "/?q=zzzz")
    assert names_in_order(html) == []
    assert html.count("Nenhuma instituição nesta seção.") == 3


# --- Demo / fonte / vencimento ------------------------------------------------


def test_demo_banner_and_badges_in_demo_mode(seeded):
    html = page(client_for(seeded))
    assert "DADOS DE DEMONSTRAÇÃO — não refletem o Downdetector" in html
    assert all("DEMO" in c for c in cards(html))


def test_demo_banner_when_latest_run_is_demo_even_if_collector_is_real(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, collector="downdetector")
    # banco real recebendo uma execucao marcada como demo (caso que o banco permite)
    add_run(cfg, NOW - 5 * MIN, LEVELS)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE runs SET is_demo = 1")
    conn.commit()
    conn.close()
    assert "DADOS DE DEMONSTRAÇÃO" in page(client_for(cfg))


def test_no_demo_banner_for_real_collector_run(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, collector="downdetector")
    add_run(cfg, NOW - 5 * MIN, LEVELS)
    html = page(client_for(cfg))
    assert "DADOS DE DEMONSTRAÇÃO" not in html and "DEMO" not in html


def test_not_authorized_notice_and_everything_no_data(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, collector="downdetector")
    add_run(cfg, NOW - 5 * MIN, status="not_authorized")
    html = page(client_for(cfg))
    assert "Coletor Downdetector pendente de autorização" in html
    assert len(cards(html)) == 4
    assert all("state-no-data" in c for c in cards(html))


def test_inaccessible_notice_and_word_never_in_cards(cfg):
    add_run(cfg, NOW - 100 * MIN, LEVELS)
    add_run(cfg, NOW - 5 * MIN, status="inaccessible", error="timeout")
    html = page(client_for(cfg))
    assert "Downdetector indisponível às 12:25" in html
    assert cards(html)
    assert not any("ndisponível" in c for c in cards(html))
    assert all("state-no-data" in c for c in cards(html))


def test_error_notice(cfg):
    add_run(cfg, NOW - 5 * MIN, status="error", error="RuntimeError: falhou")
    html = page(client_for(cfg))
    assert "Falha na última coleta às 12:25" in html
    assert "RuntimeError: falhou" in html


def test_stale_shows_notice_and_no_green_card(cfg):
    add_run(cfg, NOW - 100 * MIN, {i[0]: "no_alert" for i in INSTITUTIONS})
    html = page(client_for(cfg))
    assert "Verificação vencida desde 12:20" in html  # (NOW-100min) + 90min = 15:20 UTC
    assert not any("state-no-alert" in c for c in cards(html))
    assert all("state-no-data" in c for c in cards(html))


def test_not_stale_at_exact_limit(cfg):
    add_run(cfg, NOW - 90 * MIN, {i[0]: "no_alert" for i in INSTITUTIONS})
    html = page(client_for(cfg))
    assert "Verificação vencida" not in html
    assert all("state-no-alert" in c for c in cards(html))


def test_missing_database_all_no_data_never_green(cfg):
    assert not cfg.db_path.exists()
    html = page(client_for(cfg))
    assert len(cards(html)) == 4
    assert all("state-no-data" in c and "Sem dados" in c for c in cards(html))
    assert not any("state-no-alert" in c for c in cards(html))
    assert "init-db" in html and "collect" in html
    assert not cfg.db_path.exists()  # o dashboard nao cria o banco


def test_unreadable_database_never_green_and_no_500(cfg):
    cfg.data_dir.mkdir(parents=True)
    cfg.db_path.write_bytes(b"isto nao e um banco sqlite" * 50)
    resp = client_for(cfg).get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "ilegível" in html
    assert all("state-no-data" in c for c in cards(html))
    for url in ("/history", "/institution/alfa"):
        assert client_for(cfg).get(url).status_code == 200


def test_database_without_schema_is_handled(cfg):
    cfg.data_dir.mkdir(parents=True)
    sqlite3.connect(cfg.db_path).close()  # arquivo vazio, user_version 0
    html = page(client_for(cfg))
    assert all("state-no-data" in c for c in cards(html))


def test_dashboard_does_not_modify_database(seeded):
    before = seeded.db_path.read_bytes()
    client = client_for(seeded)
    for url in ("/", "/history?period=30d", "/institution/alfa"):
        page(client, url)
    assert seeded.db_path.read_bytes() == before


def test_open_readonly_rejects_writes(seeded):
    conn = db.open_readonly(seeded.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM runs")
    finally:
        conn.close()


def test_config_institution_not_yet_in_database_is_no_data(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    add_run(cfg, NOW - 5 * MIN, LEVELS)
    extra = INSTITUTIONS + (("novo", "Banco Novo", "digitais"),)
    cfg2 = make_cfg(tmp_path, monkeypatch, institutions=extra)  # mesmo banco, TOML maior
    html = page(client_for(cfg2))
    novo = next(c for c in cards(html) if "Banco Novo" in c)
    assert "state-no-data" in novo


# --- Historico e instituicao ---------------------------------------------------


def test_history_exposes_has_data_false_for_missing_hours(seeded):
    html = page(client_for(seeded), "/history?period=24h")
    chart = json_block(html, "history-data")
    n = len(chart["hours"])
    assert n in (24, 25)
    assert len(chart["has_data"]) == len(chart["attention"]) == len(chart["critical"]) == n
    assert chart["has_data"].count(True) == 1
    assert chart["has_data"].count(False) == n - 1
    idx = chart["has_data"].index(True)
    assert chart["critical"][idx] == 1 and chart["attention"][idx] == 1
    assert chart["demo"] is True and chart["total"] == 4


def test_history_without_runs_all_hours_no_data(cfg):
    html = page(client_for(cfg), "/history?period=7d")
    chart = json_block(html, "history-data")
    assert len(chart["hours"]) >= 168 and not any(chart["has_data"])
    assert "Sem dados" in html


@pytest.mark.parametrize("period,hours", [("24h", 24), ("7d", 168), ("30d", 720)])
def test_history_periods(seeded, period, hours):
    chart = json_block(page(client_for(seeded), f"/history?period={period}"), "history-data")
    assert len(chart["hours"]) in (hours, hours + 1)


def test_history_invalid_period_falls_back(seeded):
    chart = json_block(page(client_for(seeded), "/history?period=999d"), "history-data")
    assert len(chart["hours"]) in (24, 25)


def test_history_lists_transitions_and_failed_runs(cfg):
    add_run(cfg, NOW - 200 * MIN, {"alfa": "no_alert"})
    add_run(cfg, NOW - 140 * MIN, {"alfa": "critical"})
    add_run(cfg, NOW - 30 * MIN, status="error", error="boom")
    html = page(client_for(cfg), "/history")
    assert "Execuções com falha" in html and "Falha na coleta" in html and "boom" in html
    assert "Banco Alfa" in html
    assert 'class="state-badge state-critical"' in html


def test_history_demo_badge(seeded):
    assert "DEMO" in page(client_for(seeded), "/history")


def test_institution_timeline_data(cfg):
    add_run(cfg, NOW - 180 * MIN, {"alfa": "no_alert"})
    add_run(cfg, NOW - 60 * MIN, {"alfa": "critical"})
    html = page(client_for(cfg), "/institution/alfa?period=24h")
    data = json_block(html, "timeline-data")
    states = [s["state"] for s in data["segments"]]
    assert "no_alert" in states and "critical" in states and "no_data" in states
    assert data["segments"][0]["start"] == data["min"] and data["segments"][-1]["end"] == data["max"]
    assert "Banco Alfa" in html and "Estado atual" in html
    assert "Transições" in html


def test_institution_without_database(cfg):
    html = page(client_for(cfg), "/institution/alfa")
    data = json_block(html, "timeline-data")
    assert [s["state"] for s in data["segments"]] == ["no_data"]


def test_unknown_institution_is_friendly_404(seeded):
    resp = client_for(seeded).get("/institution/inexistente")
    assert resp.status_code == 404
    html = resp.get_data(as_text=True)
    assert "Página não encontrada" in html and "Traceback" not in html
    assert resp.headers["Content-Security-Policy"]


def test_unknown_route_404(seeded):
    assert client_for(seeded).get("/nada").status_code == 404


# --- Seguranca ----------------------------------------------------------------


EVIL = "<script>alert(1)</script>"


def test_xss_in_institution_name_is_escaped_everywhere(tmp_path, monkeypatch):
    insts = (("xss", EVIL, "digitais"), ("alfa", "Banco Alfa", "tradicionais"))
    cfg = make_cfg(tmp_path, monkeypatch, institutions=insts)
    add_run(cfg, NOW - 200 * MIN, {"xss": "no_alert", "alfa": "no_alert"})
    add_run(cfg, NOW - 100 * MIN, {"xss": "critical", "alfa": "no_alert"})
    client = client_for(cfg)
    for url in ("/", "/history", "/institution/xss"):
        html = page(client, url)
        assert EVIL not in html, url
        assert htmllib.escape(EVIL) in html, url


def test_xss_in_error_detail_is_escaped(cfg):
    add_run(cfg, NOW - 5 * MIN, status="error", error=EVIL)
    client = client_for(cfg)
    for url in ("/", "/history"):
        html = page(client, url)
        assert EVIL not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_xss_in_query_string_is_not_reflected_raw(seeded):
    html = page(client_for(seeded), "/?q=" + EVIL)
    assert EVIL not in html
    assert htmllib.escape(EVIL) in html  # reexibido escapado no campo de busca


def test_security_headers_on_all_responses(seeded):
    client = client_for(seeded)
    expected_csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    for url in ("/", "/history", "/institution/alfa", "/institution/x", "/static/app.js", "/static/app.css"):
        resp = client.get(url)
        assert resp.headers["Content-Security-Policy"] == expected_csp, url
        assert resp.headers["X-Content-Type-Options"] == "nosniff", url
        assert resp.headers["Referrer-Policy"] == "no-referrer", url
        assert resp.headers["X-Frame-Options"] == "DENY", url
        assert resp.headers["Cross-Origin-Resource-Policy"] == "same-origin", url
        assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin", url


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "127.0.0.1.evil.com", "[::2]:8765", "10.0.0.5"])
def test_untrusted_host_is_400(seeded, host):
    resp = client_for(seeded).get("/", headers={"Host": host})
    assert resp.status_code == 400
    assert resp.headers["Content-Security-Policy"]


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765", "[::1]"])
def test_trusted_hosts_are_accepted(seeded, host):
    assert client_for(seeded).get("/", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
@pytest.mark.parametrize("url", ["/", "/history", "/institution/alfa"])
def test_write_methods_are_405(seeded, method, url):
    resp = getattr(client_for(seeded), method)(url)
    assert resp.status_code == 405
    assert "Allow" in resp.headers and "POST" not in resp.headers["Allow"]


def test_options_is_405_and_head_works(seeded):
    client = client_for(seeded)
    assert client.options("/").status_code == 405
    head = client.head("/")
    assert head.status_code == 200 and head.data == b""


def test_app_is_not_in_debug_mode(seeded):
    from bankmon.web import create_app

    app = create_app(seeded)
    assert app.debug is False
    assert app.config["TRUSTED_HOSTS"] == ["127.0.0.1", "localhost"]


def test_rendered_pages_have_no_external_urls_or_inline_scripts(seeded):
    client = client_for(seeded)
    for url in ("/", "/history", "/institution/alfa", "/institution/nope"):
        html = client.get(url).get_data(as_text=True)
        assert not re.search(r"""(src|href|action)\s*=\s*["']?(https?:)?//""", html), url
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag or 'type="application/json"' in tag, (url, tag)
        assert not re.search(r"\son\w+\s*=", html), url  # sem handlers inline
        assert " style=" not in html, url


def test_repeated_filter_param_uses_first_value_only(seeded):
    assert names_in_order(page(client_for(seeded), "/?state=critical&state=attention")) == ["Banco Alfa"]
