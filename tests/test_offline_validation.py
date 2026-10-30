"""Validacao ponta a ponta (offline) do parser DOM -> SQLite -> dashboard.

HTML SINTETICO e bancos montados em tmp_path: nenhum teste deste arquivo le ou escreve em `data/`.
"""

import dataclasses
import functools
from datetime import datetime, timezone
import html as htmllib
import re
import sqlite3

import pytest

from bankmon import cli, db
from bankmon.web import create_app

ISO = "2026-10-28T12:00:00Z"
BLUE, YELLOW, RED = (f"var(--color-dd-{c})" for c in ("blue", "yellow", "red"))
SVG = 'viewBox="0 0 100 40" width="100" height="40"'

INSTITUTIONS = ("Banco Itaú", "Bradesco", "Caixa Econômica Federal", "Nubank", "Santander")


def chart(name, status="sem problemas", color=BLUE, svg=SVG, d="M0 30 L10 20"):
    label = htmllib.escape(f"Relatos de {name} nas últimas 24 horas. Status atual: {status}")
    return (f'<div role="img" aria-label="{label}"><svg {svg}>'
            f'<path d="{htmllib.escape(d)}" stroke="{color}"></path></svg></div>')


FILLERS = [chart(f"Servico {i}") for i in range(10)]


def write_html(tmp_path, charts, name="c.html"):
    path = tmp_path / name
    path.write_text("<html><body>" + "".join(charts) + "</body></html>", "utf-8")
    return path


def do_import(path, iso=ISO):
    args = ["import-html", str(path)] + (["--capturado-em", iso] if iso else [])
    return cli.main(args)


def offline_cfg(project_config):
    return cli.offline_config(project_config)


def client(cfg, now=None):
    frozen = now or cli.offline_now(cfg)
    return create_app(cfg, clock=lambda: frozen).test_client()


def states(html):
    return {name: css for css, name in re.findall(
        r'<article class="card (state-[a-z-]+)".*?<a [^>]*>(.*?)</a>', html, re.S)}


def sql(cfg, query, params=()):
    conn = sqlite3.connect(cfg.db_path)
    try:
        return [tuple(r) for r in conn.execute(query, params)]
    finally:
        conn.close()


# --- 1. migracao de um banco demo v1 (montado em tmp_path) ---------------------------


def _dump(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("runs", "observations", "state_transitions")}


def _make_demo_v1_db(cfg, path, hours=6):
    """Banco no formato do demo v1: schema v1, instituicoes do TOML, execucoes horarias do coletor
    demo (is_demo=1) com observacoes e transicoes."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text("utf-8").split(db.V2_MARKER)[0] + "PRAGMA user_version = 1;")
    for c in cfg.categories:
        conn.execute("INSERT INTO categories VALUES (?, ?, ?)", (c.id, c.name, c.order))
    for i in cfg.institutions:
        conn.execute("INSERT INTO institutions VALUES (?, ?, ?, ?, ?)",
                     (i.id, i.name, i.category, i.dd_label, int(i.enabled)))
    enabled = [i.id for i in cfg.institutions if i.enabled]
    for h in range(hours):
        at = f"2026-10-28T{6 + h:02d}:00:00Z"
        run = conn.execute(
            "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status) "
            "VALUES (?, ?, 'demo', 1, 'scheduled', 'ok')", (at, at)).lastrowid
        for n, iid in enumerate(enabled):
            level = "attention" if (n + h) % 4 == 0 else "no_alert"
            conn.execute("INSERT INTO observations VALUES (?, ?, ?, NULL)", (run, iid, level))
            if h and (n + h) % 4 in (0, 1):
                frm, to = ("no_alert", "attention") if level == "attention" else ("attention", "no_alert")
                conn.execute("INSERT INTO state_transitions (institution_id, run_id, from_state, to_state, "
                             "changed_at) VALUES (?, ?, ?, ?, ?)", (iid, run, frm, to, at))
    conn.commit()
    conn.close()


def test_migration_of_a_demo_v1_database_is_lossless(project_config, tmp_path):
    dest = tmp_path / "demo"
    dest.mkdir()
    cfg = dataclasses.replace(project_config, data_dir=dest)
    assert cfg.db_path == dest / "bankmon-demo.db"
    _make_demo_v1_db(cfg, cfg.db_path)

    conn = db.connect(cfg.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1, "o banco montado deveria ser v1"
        before = _dump(conn)
        assert len(before["runs"]) > 0 and len(before["observations"]) > 0
        assert len(before["state_transitions"]) > 0
    finally:
        conn.close()
    fixed = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)  # banco v1 nao tem run_sources
    client = functools.partial(globals()["client"], now=fixed)
    pages = ["/", "/history", "/institution/itau"]
    html_before = {}
    for p in pages:
        resp = client(cfg).get(p)
        assert resp.status_code == 200, p
        html_before[p] = resp.get_data(as_text=True)

    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"run_sources", "run_services"} <= names
        assert _dump(conn) == before
        assert conn.execute("SELECT count(*) FROM run_services").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert {p: client(cfg).get(p).get_data(as_text=True) for p in pages} == html_before


# --- 2. isolamento ---------------------------------------------------------------------


def test_import_html_touches_only_offline_db(project_config, tmp_path):
    assert do_import(write_html(tmp_path, FILLERS)) == 0
    dbs = sorted(p.name for p in project_config.data_dir.iterdir() if p.suffix == ".db")
    assert dbs == ["bankmon-offline.db"]


def test_serve_and_status_without_offline_use_demo_db(project_config, monkeypatch, capsys):
    import waitress

    import bankmon.web

    seen = {}
    monkeypatch.setattr(bankmon.web, "create_app", lambda cfg, clock=None: seen.update(cfg=cfg) or "app")
    monkeypatch.setattr(waitress, "serve", lambda *a, **k: None)
    assert cli.main(["serve"]) == 0
    assert seen["cfg"].db_path.name == "bankmon-demo.db" and not seen["cfg"].is_offline
    assert cli.main(["serve", "--offline"]) == 0
    assert seen["cfg"].db_path.name == "bankmon-offline.db" and seen["cfg"].is_offline
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "OFFLINE" not in out and "captura histórica" not in out
    assert sorted(p.name for p in project_config.data_dir.glob("*.db")) == ["bankmon-demo.db"]


# --- 3. nunca verde sem dado valido -----------------------------------------------------


def test_never_green_without_valid_data(project_config, tmp_path):
    charts = FILLERS + [
        chart("Banco Itaú", "status esquisito"),                      # texto desconhecido
        chart("Bradesco", "sem problemas", RED),                      # texto verde, cor vermelha
        # Caixa ausente
        chart("Nubank"), chart("Nubank", "enfrenta problemas", RED),  # duplicado em conflito
        chart("Santander"),                                           # unico valido
    ]
    assert do_import(write_html(tmp_path, charts)) == 0
    st = states(client(offline_cfg(project_config)).get("/").get_data(as_text=True))
    for name in ("Itaú", "Bradesco", "Caixa", "Nubank"):
        assert st[name] == "state-no-data", (name, st[name])
    assert st["Santander"] == "state-no-alert"


@pytest.mark.parametrize("kind", ["blocked", "incomplete"])
def test_blocked_or_incomplete_page_all_no_data_with_warning(project_config, tmp_path, kind):
    cfg = offline_cfg(project_config)
    good = write_html(tmp_path, FILLERS + [chart(n) for n in INSTITUTIONS], "ok.html")
    assert do_import(good, "2026-10-28T11:00:00Z") == 0
    if kind == "blocked":
        bad = tmp_path / "b.html"
        bad.write_text("<html><title>Just a moment...</title><body>" + "".join(FILLERS) + "</body></html>")
        expected = "indisponível"
    else:
        bad = write_html(tmp_path, FILLERS[:3], "b.html")
        expected = "Falha"
    assert do_import(bad, ISO) == 1
    html = client(cfg).get("/").get_data(as_text=True)
    st = states(html)
    assert len(st) == 15 and set(st.values()) == {"state-no-data"}
    assert expected in html
    assert sql(cfg, "SELECT count(*) FROM run_services WHERE run_id = (SELECT max(id) FROM runs)") == [(0,)]


# --- 4. horario de origem -------------------------------------------------------------------


def test_captured_at_semantics(project_config, tmp_path, capsys):
    cfg = offline_cfg(project_config)
    html = write_html(tmp_path, FILLERS)
    assert do_import(html, "2026-10-28T09:30:00-03:00") == 0
    assert sql(cfg, "SELECT started_at, finished_at FROM runs") == [("2026-10-28T12:30:00Z",) * 2]
    assert sql(cfg, "SELECT captured_at FROM run_sources") == [("2026-10-28T12:30:00Z",)]
    other = write_html(tmp_path, FILLERS[:9] + [chart("Outro")], "o.html")
    assert do_import(other, "2026-10-28T09:30:00") == 1  # sem fuso
    assert "fuso" in capsys.readouterr().err
    assert sql(cfg, "SELECT count(*) FROM runs") == [(1,)]  # recusado antes de gravar
    assert do_import(other, "2030-01-01T00:00:00Z") == 1  # futuro: recusado pelo CLI
    assert "intervalo" in capsys.readouterr().err
    assert sql(cfg, "SELECT count(*) FROM runs") == [(1,)]
    assert sql(cfg, "SELECT count(*) FROM run_services WHERE run_id > 1") == [(0,)]
    assert sql(cfg, "SELECT count(*) FROM run_sources") == [(1,)]
    assert sql(cfg, "SELECT count(*) FROM observations WHERE run_id > 1") == [(0,)]


# --- 5. geometria hostil -------------------------------------------------------------------------


HOSTILE_D = [
    "M0 0 <script>alert(1)</script>", 'M0 0 "x"', "M0 0 url(#a)", "M0 0 javascript:alert(1)",
    "M0 0 L nan 1", "M0 0 L1e999 1", "M0 0 " + "1 " * 10000, "M0 0 'x'",
]
HOSTILE_VB = ["0 0 0 40", "0 0 -5 40", "0 0 NaN 4", "0 0 1e999 4", "<script> 0 1 1",
              "url(javascript:x) 0 1 1", '0 0 "1" 1', "0 0 0 0"]


def test_hostile_geometry_becomes_null_with_issue_and_never_rendered(project_config, tmp_path):
    cfg = offline_cfg(project_config)
    hostile = [chart(f"H{i}", d=d, svg=f'viewBox="{htmllib.escape(v)}" width="{htmllib.escape(v)}"')
               for i, (d, v) in enumerate(zip(HOSTILE_D, HOSTILE_VB))]
    charts = FILLERS + hostile + [chart("Banco Itaú", "possíveis problemas", YELLOW)]
    assert do_import(write_html(tmp_path, charts)) == 0
    rows = sql(cfg, "SELECT service, svg_viewbox, svg_width, svg_height, svg_path_d, issue FROM run_services "
                    "WHERE service LIKE 'H%' ORDER BY position")
    assert len(rows) == len(HOSTILE_D)
    for service, vb, w, h, d, issue in rows:
        assert (vb, w, h, d) == (None,) * 4, service
        assert "svg_path_d_invalido" in issue and "svg_viewbox_invalido" in issue, (service, issue)
    assert sql(cfg, "SELECT svg_path_d FROM run_services WHERE service = 'Banco Itaú'") == [("M0 30 L10 20",)]
    c = client(cfg)
    for page in ("/", "/history", "/institution/itau"):
        body = c.get(page).get_data(as_text=True)
        if page == "/":  # so a geometria valida do Itau vira grafico; as hostis nunca
            assert body.count("<svg") == 1 and body.count('<path d="M0 30 L10 20"/>') == 1
        else:
            assert "M0 30" not in body and "<svg" not in body and "viewBox" not in body, page
        assert "javascript:" not in body


# --- 6. reimportacao -----------------------------------------------------------------------------------

TABLES = ("runs", "observations", "state_transitions", "run_sources", "run_services")


def test_reimport_same_content_other_name_is_atomic_noop(project_config, tmp_path):
    cfg = offline_cfg(project_config)
    a = write_html(tmp_path, FILLERS, "a.html")
    assert do_import(a) == 0
    snapshot = [sql(cfg, f"SELECT * FROM {t}") for t in TABLES]
    b = tmp_path / "outro-nome.html"
    b.write_bytes(a.read_bytes())
    assert do_import(b, "2026-10-29T00:00:00Z") == 1
    assert snapshot == [sql(cfg, f"SELECT * FROM {t}") for t in TABLES]


# --- 7. dashboard offline + XSS ----------------------------------------------------------------------------


XSS_NAME = "<img src=x onerror=alert(1)>"
XSS_STATUS = '<script>alert("s")</script>'


def test_offline_dashboard_pages_and_xss(project_config, tmp_path):
    cfg = offline_cfg(project_config)
    charts = FILLERS + [
        chart(XSS_NAME), chart("Banco Itaú", XSS_STATUS), chart("Bradesco", f"sem problemas {XSS_STATUS}"),
        chart("Nubank", "enfrenta problemas", RED),
    ]
    assert do_import(write_html(tmp_path, charts)) == 0
    c = client(cfg)
    pages = ["/", "/history", "/institution/itau", "/institution/bradesco", "/institution/nubank",
             "/?q=%3Cscript%3E", "/?state=%3Cx%3E"]
    for page in pages:
        resp = c.get(page)
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200, page
        for raw in ("<script>alert", "<img src=x", "onerror=alert(1)>"):
            assert raw not in body, (page, raw)
        assert "DEMONSTRAÇÃO" not in body and "vencida" not in body.lower(), page
    index = c.get("/").get_data(as_text=True)
    assert "capturado em 28/10 09:00" in index and "captura histórica" in index
    assert states(index)["Nubank"] == "state-critical"


def test_hostile_observation_detail_and_error_detail_are_escaped(project_config):
    from datetime import datetime, timezone

    from bankmon import runner
    from bankmon.collectors.base import CollectionResult, Observation

    cfg = offline_cfg(project_config)
    at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    conn = db.open_db(cfg)
    try:
        for i, (status, detail) in enumerate((("ok", None), ("error", XSS_STATUS + XSS_NAME))):
            obs = (Observation("itau", "attention", XSS_STATUS + XSS_NAME),) if status == "ok" else ()
            res = CollectionResult("offline_html", False, at.replace(minute=i), at.replace(minute=i),
                                   status, obs, detail)
            runner.record_result(conn, cfg, res, "cli")
    finally:
        conn.close()
    c = client(cfg)
    for page in ("/", "/history", "/institution/itau"):
        body = c.get(page).get_data(as_text=True)
        assert "<script>alert" not in body and "<img src=x" not in body, page
