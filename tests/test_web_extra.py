"""E1.2 (ampliacao QA): contadores, estados degradados, fuso, historico, ids hostis,
cabecalhos, somente-leitura. Complementa tests/test_web.py."""

from __future__ import annotations

import html as htmllib
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from bankmon import db, status
from bankmon.timeutil import utc_now
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
ALL_OK = {i[0]: "no_alert" for i in INSTITUTIONS}
EVIL = "<script>alert(1)</script>"
EVIL2 = '"><img src=x onerror=alert(1)>'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch)


@pytest.fixture
def seeded(cfg):
    add_run(cfg, NOW - 10 * MIN, LEVELS)
    return cfg


def names(html):
    return re.findall(r'<h4 class="card-name"><a [^>]*>(.*?)</a>', html)


def count_items(html):
    """{rotulo: n} do bloco de contadores."""
    return {
        m[1].strip(): int(m[0])
        for m in re.findall(r'<span class="count-n">(\d+)</span>\s*([^<]+)</li>', html)
    }


def section_counts(html):
    return [int(n) for n in re.findall(r'class="section-count">\((\d+)\)', html)]


# --- 1. Contadores e filtros --------------------------------------------------


def test_counters_match_cards(seeded):
    html = page(client_for(seeded))
    c = count_items(html)
    assert c == {"Crítico": 1, "Atenção": 1, "Sem alerta": 1, "Sem dados": 1, "Não monitorado": 0}
    assert sum(c.values()) == len(cards(html)) == 4
    assert section_counts(html) == [2, 1, 1]


def test_counters_are_global_while_sections_follow_filters(seeded):
    html = page(client_for(seeded), "/?state=critical")
    assert sum(count_items(html).values()) == 4  # resumo nao muda com o filtro
    assert section_counts(html) == [1, 0, 0]
    assert len(cards(html)) == 1


@pytest.mark.parametrize(
    "qs,expected",
    [
        ("category=tradicionais&state=critical&q=alfa", ["Banco Alfa"]),
        ("category=digitais&state=critical", []),  # combinacao valida sem resultado
        ("category=tradicionais&state=bogus", ["Banco Alfa", "Banco Beta"]),  # estado invalido ignorado
        ("category=bogus&state=attention&q=", ["Banco Beta"]),
        ("q=%20%20banco%20%20gama%20", ["Banco Gama"]),  # espacos normalizados
        ("q=BANCO", ["Banco Alfa", "Banco Beta", "Banco Gama", "Banco Delta"]),
        ("state=not_monitored", []),  # ausente da pagina agora e "Sem dados"
        ("state=no_data", ["Banco Delta"]),
    ],
)
def test_filter_combinations(seeded, qs, expected):
    assert names(page(client_for(seeded), "/?" + qs)) == expected


@pytest.mark.parametrize(
    "qs", ["q=%00", "q=%FF%FE", "state=%00", "category=%E2%80%AE", "q=%27%22%3C%3E", "state[]=x", "q=" + "%C3%A9" * 300]
)
def test_hostile_filters_never_500(seeded, qs):
    assert client_for(seeded).get("/?" + qs).status_code == 200


def test_filter_chip_links_preserve_other_filters(seeded):
    html = page(client_for(seeded), "/?category=digitais&q=gama")
    assert 'href="/?category=digitais&amp;q=gama"' in html or 'href="/?category=digitais&q=gama"' in html


# --- 2. Nunca verde sem dado valido ------------------------------------------


def _no_green(html):
    cs = cards(html)
    assert len(cs) == 4
    assert all("state-no-data" in c and "Sem dados" in c for c in cs)
    assert not any("state-no-alert" in c or "Sem alerta" in c for c in cs)
    assert count_items(html)["Sem alerta"] == 0
    assert section_counts(html) == [0, 0, 4]


def test_empty_existing_database(cfg):
    db.open_db(cfg).close()
    html = page(client_for(cfg))
    _no_green(html)
    assert "Nenhuma verificação válida" in html


def test_missing_database(cfg):
    _no_green(page(client_for(cfg)))


def test_corrupted_database_file(cfg):
    cfg.data_dir.mkdir(parents=True)
    cfg.db_path.write_bytes(b"\x00\xffGARBAGE" * 4000)
    client = client_for(cfg)
    _no_green(page(client))
    for url in ("/history", "/history?period=30d", "/institution/alfa"):
        assert client.get(url).status_code == 200, url
    assert "SQLite" not in page(client) and "Traceback" not in page(client)  # sem vazar detalhe interno


def test_truncated_real_database(seeded):
    data = seeded.db_path.read_bytes()
    seeded.db_path.write_bytes(data[:700])
    for u in ("/", "/history"):
        assert client_for(seeded).get(u).status_code == 200


@pytest.mark.parametrize(
    "status_,expected",
    [
        ("error", "Falha na última coleta às 12:25"),
        ("inaccessible", "Downdetector indisponível às 12:25"),
        ("not_authorized", "Coletor Downdetector pendente de autorização"),
    ],
)
def test_latest_failed_run_notice_and_no_green_even_with_recent_ok(cfg, status_, expected):
    add_run(cfg, NOW - 20 * MIN, ALL_OK)
    add_run(cfg, NOW - 5 * MIN, status=status_, error="x")
    html = page(client_for(cfg))
    assert expected in html
    assert not any("state-no-alert" in c for c in cards(html))
    assert all("state-no-data" in c for c in cards(html))


def test_stale_data_notice_and_boundary_91_minutes(cfg):
    add_run(cfg, NOW - 91 * MIN, ALL_OK)
    html = page(client_for(cfg))
    from zoneinfo import ZoneInfo

    expected = (NOW - 91 * MIN + 90 * MIN).astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%H:%M")
    assert f"Verificação vencida desde {expected}" in html  # calculado via zoneinfo (12:29)
    assert not any("state-no-alert" in c for c in cards(html))


def test_new_institution_in_toml_shows_as_no_data_not_green(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    add_run(cfg, NOW - 5 * MIN, ALL_OK)
    cfg2 = make_cfg(tmp_path, monkeypatch, institutions=INSTITUTIONS + (("novo", "Banco Novo", "digitais"),))
    cs = cards(page(client_for(cfg2)))
    novo = next(c for c in cs if "Banco Novo" in c)
    assert "state-no-data" in novo
    assert sum("state-no-alert" in c for c in cs) == 4


def test_institution_removed_from_toml_disappears(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    add_run(cfg, NOW - 5 * MIN, ALL_OK)
    cfg2 = make_cfg(tmp_path, monkeypatch, institutions=INSTITUTIONS[:2])
    html = page(client_for(cfg2))
    assert names(html) == ["Banco Alfa", "Banco Beta"]
    assert client_for(cfg2).get("/institution/gama").status_code == 404


# --- 3. "Indisponivel" apenas no aviso da fonte -----------------------------------


@pytest.mark.parametrize("status_", ["error", "inaccessible", "not_authorized"])
def test_unavailable_word_never_inside_any_card(cfg, status_):
    add_run(cfg, NOW - 100 * MIN, LEVELS)
    add_run(cfg, NOW - 5 * MIN, status=status_, error="falha indisponivel")
    html = page(client_for(cfg))
    for c in cards(html):
        assert not re.search(r"(?i)indispon", c), c
    if status_ == "inaccessible":
        assert re.search(r'class="notice notice-source"[^>]*>Downdetector indisponível', html)


def test_unavailable_word_not_in_cards_for_every_state(seeded):
    for url in ("/", "/?state=critical", "/?state=no_data"):
        for c in cards(page(client_for(seeded), url)):
            assert "ndispon" not in c


# --- 4. DEMO ------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/", "/history", "/institution/alfa"])
def test_demo_banner_in_every_page_in_demo_mode(seeded, url):
    html = page(client_for(seeded), url)
    assert 'class="demo-banner"' in html and "DEMO" in html


def test_demo_banner_on_404_in_demo_mode(seeded):
    assert 'class="demo-banner"' in client_for(seeded).get("/institution/zzz").get_data(as_text=True)


@pytest.mark.parametrize("url", ["/", "/history", "/institution/alfa", "/institution/zzz"])
def test_no_demo_marks_in_real_mode(tmp_path, monkeypatch, url):
    cfg = make_cfg(tmp_path, monkeypatch, collector="downdetector")
    add_run(cfg, NOW - 5 * MIN, LEVELS)
    assert cfg.db_path.name == "bankmon.db"
    resp = client_for(cfg).get(url)
    html = resp.get_data(as_text=True)
    assert "demo-banner" not in html and "DEMO" not in html and "demonstra" not in html.lower()


def test_real_mode_does_not_read_demo_database(tmp_path, monkeypatch):
    demo = make_cfg(tmp_path, monkeypatch)
    add_run(demo, NOW - 5 * MIN, LEVELS)
    real = make_cfg(tmp_path, monkeypatch, collector="downdetector")
    assert demo.db_path != real.db_path and demo.db_path.exists() and not real.db_path.exists()
    html = page(client_for(real))
    assert "DEMO" not in html and all("state-no-data" in c for c in cards(html))


# --- 5. Historico -----------------------------------------------------------------


@pytest.fixture
def history_cfg(cfg):
    add_run(cfg, NOW - 300 * MIN, ALL_OK)
    add_run(cfg, NOW - 180 * MIN, {**ALL_OK, "alfa": "critical", "beta": "attention"})
    add_run(cfg, NOW - 120 * MIN, status="inaccessible", error="timeout")
    add_run(cfg, NOW - 30 * MIN, {**ALL_OK, "alfa": "attention"})
    return cfg


def test_history_json_matches_runs(history_cfg):
    chart = json_block(page(client_for(history_cfg), "/history?period=24h"), "history-data")
    n = len(chart["hours"])
    assert n in (24, 25) and all(len(chart[k]) == n for k in ("has_data", "attention", "critical"))
    # horas locais = UTC-3; horas com execucao ok: 10:30Z->07:30Z... usa o rotulo
    by_hour = {h: (a, c, d) for h, a, c, d in zip(chart["hours"], chart["attention"], chart["critical"], chart["has_data"])}
    expected = {  # hora UTC -> (atencao, critico)
        NOW - 300 * MIN: (0, 0),
        NOW - 180 * MIN: (1, 1),
        NOW - 30 * MIN: (1, 0),
    }
    for moment, (att, crit) in expected.items():
        label = (moment - timedelta(hours=3)).strftime("%d/%m %H:00")
        assert by_hour[label] == (att, crit, True), label
    # hora da execucao com falha nao tem execucao ok -> has_data False
    failed_label = ((NOW - 120 * MIN) - timedelta(hours=3)).strftime("%d/%m %H:00")
    assert by_hour[failed_label][2] is False
    assert chart["has_data"].count(True) == 3


def test_history_lists_transitions_and_failures_with_local_times(history_cfg):
    html = page(client_for(history_cfg), "/history")
    assert "Downdetector inacessível" in html and "timeout" in html
    assert "28/10 10:30" in html
    assert html.count("Banco Alfa") >= 2


@pytest.mark.parametrize("period", ["", "1h", "90d", "24H", "../x", "%00", "7d;drop"])
def test_history_invalid_period_defaults_to_24h_with_200(history_cfg, period):
    resp = client_for(history_cfg).get("/history", query_string={"period": period})
    assert resp.status_code == 200
    chart = json_block(resp.get_data(as_text=True), "history-data")
    assert len(chart["hours"]) in (24, 25)


def test_history_active_period_chip(history_cfg):
    html = page(client_for(history_cfg), "/history?period=7d")
    assert re.search(r'chip chip-active" href="/history\?period=7d" aria-current="page"', html)


def test_history_error_detail_is_sanitized_and_escaped(cfg):
    secret = "tok-SUPERSECRET123"
    detail = f"{EVIL} {EVIL2} https://user:pw@host/x?token={secret} Authorization: Bearer {secret}"
    add_run(cfg, NOW - 5 * MIN, status="error", error=detail)
    client = client_for(cfg)
    for url in ("/", "/history"):
        html = page(client, url)
        assert EVIL not in html and EVIL2 not in html
        assert "<img" not in html
        assert secret not in html and "user:pw" not in html, url
        assert "&lt;script&gt;" in html


def test_history_failed_detail_strips_control_chars_and_truncates(cfg):
    add_run(cfg, NOW - 5 * MIN, status="error", error="a\x1b[31mred\x07\n" + "x" * 5000)
    html = page(client_for(cfg), "/history")
    assert "\x1b" not in html and "\x07" not in html
    assert "x" * 400 not in html


def test_history_json_is_safe_when_names_are_hostile(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, institutions=(("xss", EVIL + EVIL2, "digitais"), ("alfa", "A", "tradicionais")))
    add_run(cfg, NOW - 5 * MIN, {"xss": "critical", "alfa": "no_alert"})
    html = page(client_for(cfg), "/history")
    block = re.search(r'<script type="application/json" id="history-data">(.*?)</script>', html, re.S).group(1)
    assert "<" not in block and ">" not in block  # tojson escapa para <
    json.loads(block)


# --- 6. Pagina da instituicao -------------------------------------------------------


def _expected_segments(cfg, institution_id, period, now):
    start = now - period
    conn = db.open_readonly(cfg.db_path)
    try:
        before = db.run_before(conn, start)
        runs = ([before] if before else []) + db.runs_between(conn, start, now)
    finally:
        conn.close()
    return status.state_timeline(institution_id, runs, start, now, timedelta(minutes=cfg.stale_after_minutes))


@pytest.mark.parametrize("period,delta", [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))])
@pytest.mark.parametrize("inst", ["alfa", "delta"])
def test_institution_timeline_matches_state_timeline(history_cfg, period, delta, inst):
    data = json_block(page(client_for(history_cfg), f"/institution/{inst}?period={period}"), "timeline-data")
    exp = _expected_segments(history_cfg, inst, delta, NOW)
    ms = lambda d: int(d.timestamp() * 1000)
    assert [(s["state"], s["start"], s["end"]) for s in data["segments"]] == [
        (s.state, ms(s.start), ms(s.end)) for s in exp
    ]
    assert data["min"] == ms(NOW - delta) and data["max"] == ms(NOW)
    # contiguo e coberto
    segs = data["segments"]
    assert all(a["end"] == b["start"] for a, b in zip(segs, segs[1:]))


def test_institution_timeline_gaps_are_no_data_and_labelled(history_cfg):
    data = json_block(page(client_for(history_cfg), "/institution/alfa"), "timeline-data")
    gaps = [s for s in data["segments"] if s["state"] == "no_data"]
    assert gaps and all(s["label"] == "Sem dados" for s in gaps)
    assert data["segments"][-1]["end"] == data["max"]


def test_institution_current_state_matches_dashboard_card(history_cfg):
    html_i = page(client_for(history_cfg), "/institution/alfa")
    html_i_state = re.search(r"Estado atual: <span class=\"state-badge (state-[a-z-]+)\"", html_i).group(1)
    card = next(c for c in cards(page(client_for(history_cfg))) if "Banco Alfa" in c)
    assert html_i_state in card


@pytest.mark.parametrize(
    "path",
    [
        "/institution/../etc/passwd",
        "/institution/%2e%2e%2fsecret",
        "/institution/..%2f..%2f",
        "/institution/%00",
        "/institution/alfa%00",
        "/institution/%3Cscript%3Ealert(1)%3C/script%3E",
        "/institution/" + "a" * 5000,
        "/institution/ALFA",
        "/institution/alfa/",
        "/institution/%E2%80%AE",
        "/institution/' OR 1=1--",
        "/institution/",
    ],
)
def test_hostile_institution_ids_give_404_without_error(seeded, path):
    resp = client_for(seeded).get(path)
    assert resp.status_code == 404, (path, resp.status_code)
    body = resp.get_data(as_text=True)
    assert "Traceback" not in body and "<script>alert" not in body
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_404_page_is_friendly_html_with_link_home(seeded):
    resp = client_for(seeded).get("/institution/nope")
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "Página não encontrada" in body and 'href="/"' in body


def test_institution_xss_name_in_timeline_json(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, institutions=(("xss", EVIL + EVIL2, "digitais"),))
    add_run(cfg, NOW - 5 * MIN, {"xss": "critical"})
    html = page(client_for(cfg), "/institution/xss")
    assert EVIL not in html and EVIL2 not in html and "<img" not in html
    block = re.search(r'id="timeline-data">(.*?)</script>', html, re.S).group(1)
    assert "<" not in block
    assert htmllib.escape(EVIL) in html


# --- 7. Fuso horario -----------------------------------------------------------------


def test_times_use_sao_paulo_across_utc_midnight(cfg):
    now = datetime(2026, 10, 28, 1, 30, tzinfo=timezone.utc)  # 22:30 de 27/10 local
    ran = datetime(2026, 10, 28, 1, 20, tzinfo=timezone.utc)  # 22:20 de 27/10 local
    add_run(cfg, ran, {**ALL_OK, "alfa": "critical"})
    client = client_for(cfg, now)
    html = page(client)
    assert "27/10 22:20 (há 10 min)" in html
    assert "Próxima verificação prevista:</strong> 23:20" in html
    alfa = next(c for c in cards(html) if "Banco Alfa" in c)
    assert "desde 22:20" in alfa  # mesmo dia local que `now` -> so HH:MM
    assert "28/10" not in html.split("Última verificação válida:")[1].split("</p>")[0]
    h = page(client, "/history")
    assert "27/10 22:20" in h and "28/10 01:20" not in h


def test_since_includes_date_when_transition_was_on_previous_local_day(cfg):
    ran = datetime(2026, 10, 28, 1, 20, tzinfo=timezone.utc)  # 27/10 22:20 local
    add_run(cfg, ran, {**ALL_OK, "alfa": "critical"})
    now = ran + timedelta(minutes=80)  # 02:40Z = 23:40 local, mesmo dia local
    assert "desde 22:20" in next(c for c in cards(page(client_for(cfg, now))) if "Alfa" in c)
    now = ran + timedelta(hours=3)  # 04:20Z = 01:20 local de 28/10: virou o dia local
    alfa = next(c for c in cards(page(client_for(cfg, now))) if "Alfa" in c)
    assert "desde" not in alfa or "27/10 22:20" in alfa  # dado ja vencido: sem 'desde' enganoso


def test_since_hidden_when_state_changed_since_transition(cfg):
    add_run(cfg, NOW - 100 * MIN, {**ALL_OK, "alfa": "critical"})
    alfa = next(c for c in cards(page(client_for(cfg))) if "Alfa" in c)  # venceu -> no_data
    assert "state-no-data" in alfa and "desde" not in alfa


def test_utc_hour_never_leaks_into_display(cfg):
    add_run(cfg, datetime(2026, 10, 28, 15, 20, tzinfo=timezone.utc), ALL_OK)
    html = page(client_for(cfg, datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)))
    assert "12:20" in html and "15:20" not in html


# --- 8. Seguranca HTTP --------------------------------------------------------------------

HEADERS = ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options",
    "Cross-Origin-Resource-Policy", "Cross-Origin-Opener-Policy")


@pytest.mark.parametrize(
    "method,url,code",
    [
        ("get", "/", 200),
        ("get", "/history", 200),
        ("get", "/nada", 404),
        ("get", "/institution/zzz", 404),
        ("get", "/static/nao-existe.js", 404),
        ("get", "/static/app.css", 200),
        ("get", "/static/vendor/chartjs/chart.umd.min.js", 200),
        ("post", "/", 405),
        ("put", "/history", 405),
        ("delete", "/institution/alfa", 405),
        ("options", "/", 405),
        ("post", "/static/app.js", 405),
        ("options", "/static/app.js", 405),
    ],
)
def test_four_security_headers_everywhere(seeded, method, url, code):
    resp = getattr(client_for(seeded), method)(url)
    assert resp.status_code == code
    for h in HEADERS:
        assert resp.headers.get(h), (h, url)
    assert "form-action 'self'" in resp.headers["Content-Security-Policy"]


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1.evil.com", "[::2]", "[::2]:8765", "localhost.evil.com", "127.0.0.1@evil.com", "", "[::1]x", "[::1].evil.com"])
def test_untrusted_hosts_400(seeded, host):
    resp = client_for(seeded).get("/", headers={"Host": host})
    assert resp.status_code == 400, host
    for h in HEADERS:
        assert resp.headers.get(h)


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost", "[::1]:8765", "[::1]:1"])
def test_trusted_hosts_200(seeded, host):
    assert client_for(seeded).get("/", headers={"Host": host}).status_code == 200


def test_untrusted_host_gets_no_dashboard_content(seeded):
    body = client_for(seeded).get("/", headers={"Host": "evil.com"}).get_data(as_text=True)
    assert "Banco Alfa" not in body


def test_html_pages_are_not_cacheable_but_static_is_not_forced_no_store(seeded):
    client = client_for(seeded)
    assert client.get("/").headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
def test_405_has_friendly_page_and_allow_without_write_verbs(seeded, method):
    resp = getattr(client_for(seeded), method)("/")
    assert resp.status_code == 405
    assert "somente leitura" in resp.get_data(as_text=True)
    allow = resp.headers.get("Allow", "")
    assert "GET" in allow and not {"POST", "PUT", "DELETE", "PATCH"} & set(allow.replace(" ", "").split(","))


def test_post_body_is_ignored_and_changes_nothing(seeded):
    before = seeded.db_path.read_bytes()
    resp = client_for(seeded).post("/", data={"q": "x", "state": "critical"})
    assert resp.status_code == 405 and seeded.db_path.read_bytes() == before


def test_xss_in_both_fields_across_pages_and_json(tmp_path, monkeypatch):
    insts = (("xss", EVIL2 + EVIL, "digitais"), ("alfa", "Banco Alfa", "tradicionais"))
    cfg = make_cfg(tmp_path, monkeypatch, institutions=insts)
    add_run(cfg, NOW - 200 * MIN, {"xss": "no_alert", "alfa": "no_alert"})
    add_run(cfg, NOW - 100 * MIN, {"xss": "critical", "alfa": "no_alert"})
    add_run(cfg, NOW - 5 * MIN, status="error", error=EVIL + EVIL2)
    client = client_for(cfg)
    for url in ("/", "/history", "/history?period=7d", "/institution/xss"):
        html = page(client, url)
        assert EVIL not in html and EVIL2 not in html and "<img" not in html, url
        for block in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
            assert "<" not in block
            json.loads(block)


def test_responses_have_no_server_internals(seeded):
    resp = client_for(seeded).get("/")
    assert "Werkzeug" not in resp.headers.get("X-Powered-By", "")


# --- 9. Somente leitura -------------------------------------------------------------------


def _snapshot(dirpath):
    """Arquivos do diretorio de dados -> (tamanho, mtime).

    Antivirus (aqui, Kaspersky) cria e apaga em milissegundos copias temporarias como
    `BANKMON-DEMO.DB-SHM.tmp` ao lado dos arquivos do SQLite. Elas podem sumir entre o
    iterdir() e o stat() (FileNotFoundError intermitente); nao sao do app e sao ignoradas.
    O que o teste garante (bytes e tamanho do banco, nenhum arquivo novo alem de -wal/-shm)
    continua estrito.
    """
    snap = {}
    for p in sorted(dirpath.iterdir()):
        if p.suffix.lower() == ".tmp":
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue  # sumiu durante a listagem (arquivo transitorio de terceiros)
        snap[p.name] = (st.st_size, st.st_mtime_ns)
    return snap


def test_snapshot_ignores_third_party_transient_files(tmp_path, monkeypatch):
    (tmp_path / "bankmon-demo.db").write_bytes(b"x")
    (tmp_path / "BANKMON-DEMO.DB-SHM.tmp").write_bytes(b"y")  # copia de antivirus
    sumiu = tmp_path / "bankmon-demo.db-wal"
    sumiu.write_bytes(b"z")
    real_stat = type(sumiu).stat

    def stat_que_some(self, *a, **kw):  # simula o arquivo apagado entre iterdir() e stat()
        if self.name == sumiu.name:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(type(sumiu), "stat", stat_que_some)
    assert set(_snapshot(tmp_path)) == {"bankmon-demo.db"}


def test_get_all_routes_never_changes_database_files(history_cfg):
    before = _snapshot(history_cfg.data_dir)
    content_before = history_cfg.db_path.read_bytes()
    client = client_for(history_cfg)
    for url in ("/", "/?state=critical", "/history?period=7d", "/history?period=30d", "/institution/alfa", "/institution/zzz"):
        client.get(url)
    after = _snapshot(history_cfg.data_dir)
    main = history_cfg.db_path.name
    assert history_cfg.db_path.read_bytes() == content_before  # conteudo identico
    assert after[main][0] == before[main][0]  # tamanho identico
    assert set(after) - set(before) <= {main + "-shm", main + "-wal"}  # nenhum arquivo estranho
    conn = sqlite3.connect(history_cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
    finally:
        conn.close()


def test_missing_database_remains_missing_after_get_on_every_route(cfg):
    assert not cfg.data_dir.exists()
    client = client_for(cfg)
    for url in ("/", "/history", "/institution/alfa", "/institution/zzz", "/static/app.js"):
        client.get(url)
    assert not cfg.db_path.exists()
    assert not cfg.data_dir.exists()  # nem o diretorio de dados foi criado


def test_dashboard_works_while_collector_holds_a_write_transaction(seeded):
    writer = db.connect(seeded.db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM runs")  # nao confirmado: leitor WAL ve o estado antigo
        html = page(client_for(seeded))
        assert any("state-critical" in c for c in cards(html))
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_open_readonly_missing_file_does_not_create_it(tmp_path):
    target = tmp_path / "x.db"
    with pytest.raises(sqlite3.OperationalError):
        db.open_readonly(target)
    assert not target.exists()


def test_open_readonly_blocks_every_write_statement(seeded):
    conn = db.open_readonly(seeded.db_path)
    try:
        for sql in (
            "DELETE FROM runs",
            "UPDATE institutions SET enabled = 0",
            "DROP TABLE runs",
            "PRAGMA user_version = 9",
            "INSERT INTO categories (id, name, sort_order) VALUES ('z','z',1)",
        ):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(sql)
    finally:
        conn.close()


def test_web_modules_use_no_write_sql():
    from pathlib import Path

    import bankmon.web as web_pkg

    for path in Path(web_pkg.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|open_db|init_db|sync_config)\b", text), path


# --- Config de relogio -----------------------------------------------------------------------


def test_default_clock_is_real_utc_now(seeded):
    from bankmon.web import create_app

    resp = create_app(seeded).test_client().get("/")
    assert resp.status_code == 200  # sem clock injetado; dado de 2026-10-28 pode estar vencido, sem erro
    assert utc_now().tzinfo is not None


def test_uppercase_localhost_host_should_be_accepted(seeded):
    for host in ("LOCALHOST", "LocalHost:8765", "127.0.0.1", "[::1]:8765"):
        assert client_for(seeded).get("/", headers={"Host": host}).status_code == 200, host
    for host in ("EVIL.com", "LOCALHOST.", "127.0.0.1.EVIL.com", "[::1]X"):
        assert client_for(seeded).get("/", headers={"Host": host}).status_code == 400, host


def test_static_path_traversal_is_404_and_options_405(seeded):
    client = client_for(seeded)
    for url in ("/static/../views.py", "/static/%2e%2e/views.py", "/static/..%2fviews.py", "/static//etc/passwd"):
        assert client.get(url, follow_redirects=True).status_code in (400, 404), url
    assert client.options("/static/app.js").status_code == 405
    assert client.head("/static/app.js").status_code == 200


def test_search_is_a_real_get_form_working_without_js(seeded):
    html = page(client_for(seeded), "/?category=digitais&state=critical&q=gama")
    form = re.search(r"<form[^>]*>.*?</form>", html, re.S).group(0)
    assert 'method="get"' in form and 'action="/"' in form
    assert 'name="q"' in form and 'name="category" value="digitais"' in form
    assert 'name="state" value="critical"' in form
    assert not re.search(r'method="post"', html, re.I)
    app_js = (Path(__file__).resolve().parents[1] / "src/bankmon/web/static/app.js").read_text("utf-8")
    assert "keydown" not in app_js  # busca nao depende de JS


# --- Dado malformado em banco valido (SEC-010) -------------------------------------------------


def _corrupt(cfg, sql):
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


MALFORMED = [
    "UPDATE runs SET finished_at = 'lixo'",
    "UPDATE state_transitions SET to_state = 'desconhecido'",
    "UPDATE state_transitions SET from_state = 'xyz'",
]


@pytest.mark.parametrize("sql", MALFORMED)
@pytest.mark.parametrize("url", ["/", "/history", "/history?period=30d", "/institution/alfa"])
def test_malformed_rows_degrade_to_unreadable_db_never_500_never_green(history_cfg, sql, url):
    _corrupt(history_cfg, sql)
    client = client_for(history_cfg)
    resp = client.get(url)
    assert resp.status_code == 200, (sql, url)
    body = resp.get_data(as_text=True)
    assert "Banco de dados ilegível" in body
    assert "Traceback" not in body and "lixo" not in body
    if url == "/":
        _no_green(body)
    if url == "/institution/alfa":
        assert "Sem dados" in body


def test_malformed_transition_timestamp_on_index_is_unreadable_not_500(history_cfg):
    _corrupt(history_cfg, "UPDATE state_transitions SET changed_at = 'lixo'")
    body = page(client_for(history_cfg))
    assert "Banco de dados ilegível" in body
    _no_green(body)
