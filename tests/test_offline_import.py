"""Integracao offline: parser (geometria) -> coletor -> schema v2 -> import-html -> dashboard.

Usa apenas HTML SINTETICO em tmp_path (nunca o HTML real do usuario).
"""

import dataclasses
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bankmon import cli, db, runner
from bankmon.collectors import registry
from bankmon.collectors.base import CollectionResult, InstitutionRef, ServiceRecord, SourceInfo
from bankmon.collectors.dd_parser import MAX_BYTES, parse_html
from bankmon.collectors.offline_html import OfflineHtmlCollector
from bankmon.web import create_app

CAPTURED = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)  # 09:00 em Sao Paulo
BLUE, YELLOW, RED = (f"var(--color-dd-{c})" for c in ("blue", "yellow", "red"))
SVG = 'viewBox="0 0 100 40" width="100" height="40"'
PATH_D = "M0 30 L10 20 L20 25"


def chart(name, status="sem problemas", color=BLUE, svg=SVG, d=PATH_D):
    label = f"Relatos de {name} nas últimas 24 horas. Status atual: {status}"
    return (f'<div role="img" aria-label="{label}"><svg {svg}>'
            f'<path d="{d}" stroke="{color}"></path></svg></div>')


FILLERS = [chart(f"Servico {i}") for i in range(7)]
BASE = [
    chart("Banco Itaú", "possíveis problemas", YELLOW),
    chart("Bradesco"),
    chart("Caixa Econômica Federal"),
    chart("Nubank", "enfrenta problemas", RED),
    chart("Santander", "status esquisito"),  # estado desconhecido -> sem observacao
]


def write_html(tmp_path, charts=None, name="captura.html") -> Path:
    path = tmp_path / name
    body = "".join(FILLERS + BASE if charts is None else charts)
    path.write_text(f"<html><body>{body}</body></html>", "utf-8")
    return path


REFS = [InstitutionRef(i, n, n) for i, n in (
    ("itau", "Banco Itaú"), ("bradesco", "Bradesco"), ("caixa", "Caixa Econômica Federal"),
    ("nubank", "Nubank"), ("santander", "Santander"), ("c6", "C6 Bank"),
)]


def collect(path, refs=REFS):
    return OfflineHtmlCollector(path, CAPTURED).collect(refs)


# --- parser --------------------------------------------------------------------


def test_parser_extracts_svg_geometry_of_first_path_inside_chart():
    html = chart("A", d="M1 2 L3 4") + chart("B", svg="", d="M5 5")
    services = parse_html(html)["services"]
    assert services[0]["svg"] == {"viewbox": "0 0 100 40", "width": "100", "height": "40", "path_d": "M1 2 L3 4"}
    assert services[1]["svg"] == {"viewbox": None, "width": None, "height": None, "path_d": "M5 5"}


def test_parser_ignores_svg_and_path_outside_chart():
    outside = '<div><svg viewBox="0 0 9 9" width="9"><path d="M0 0 L9 9"></path></svg></div>'
    svc = parse_html(outside + chart("A", svg="", d=""))["services"][0]
    assert svc["svg"] == {"viewbox": None, "width": None, "height": None, "path_d": None}


# --- coletor -------------------------------------------------------------------


def test_collector_maps_by_dd_label_and_records_everything(tmp_path):
    path = write_html(tmp_path)
    res = collect(path)
    assert res.source_status == "ok" and res.is_demo is False
    assert res.started_at == res.finished_at == CAPTURED
    levels = {o.institution_id: o.level for o in res.observations}
    assert levels == {"itau": "attention", "bradesco": "no_alert", "caixa": "no_alert",
                      "nubank": "critical", "c6": "not_listed"}  # santander (unknown): sem observacao
    detail = {o.institution_id: o.detail for o in res.observations}
    assert detail["itau"] == "Downdetector (HTML offline): possíveis problemas"
    assert len(res.services) == 12
    itau = next(s for s in res.services if s.service == "Banco Itaú")
    assert (itau.institution_id, itau.status, itau.color) == ("itau", "warning", "yellow")
    assert (itau.svg_viewbox, itau.svg_width, itau.svg_height, itau.svg_path_d) == (
        "0 0 100 40", "100", "40", PATH_D)
    assert next(s for s in res.services if s.service == "Servico 0").institution_id is None
    santander = next(s for s in res.services if s.service == "Santander")
    assert santander.status == "unknown" and santander.issue == "estado_textual_desconhecido"
    assert res.source == SourceInfo(path.name, hashlib.sha256(path.read_bytes()).hexdigest(),
                                    path.stat().st_size, CAPTURED)
    assert str(tmp_path) not in res.source.file_name


def test_collector_duplicate_with_conflicting_states_has_no_observation(tmp_path):
    path = write_html(tmp_path, FILLERS + [chart("Bradesco"), chart("Bradesco", "enfrenta problemas", RED),
                                          chart("Nubank"), chart("Nubank")])
    levels = {o.institution_id: o.level for o in collect(path).observations}
    assert "bradesco" not in levels and levels["nubank"] == "no_alert"


def test_collector_invalid_geometry_becomes_none_with_issue(tmp_path):
    bad = chart("Bradesco", svg='viewBox="0 0 0 40" width="abc"', d="M0 0 L9 9 script")
    res = collect(write_html(tmp_path, FILLERS + [bad, chart("Y1"), chart("Y2")]))
    svc = next(s for s in res.services if s.service == "Bradesco")
    assert (svc.svg_viewbox, svc.svg_width, svc.svg_path_d) == (None, None, None)
    assert svc.issue == "svg_viewbox_invalido; svg_width_invalido; svg_path_d_invalido"
    assert svc.status == "normal"


def test_collector_blocked_page_is_inaccessible(tmp_path):
    path = tmp_path / "b.html"
    path.write_text("<html><head><title>Just a moment...</title></head><body>"
                    + "".join(FILLERS * 2) + "</body></html>", "utf-8")
    res = collect(path)
    assert res.source_status == "inaccessible" and "blocked_by_cloudflare" in res.error_detail
    assert res.observations == () and res.services == () and res.source is None


def test_collector_incomplete_page_is_error(tmp_path):
    res = collect(write_html(tmp_path, FILLERS[:3]))
    assert res.source_status == "error" and "incompleta" in res.error_detail


def test_collector_file_over_5mb_is_error(tmp_path):
    path = tmp_path / "big.html"
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    res = collect(path)
    assert res.source_status == "error" and "5 MB" in res.error_detail


def test_collector_more_than_1000_charts_is_error(tmp_path):
    res = collect(write_html(tmp_path, [chart(f"S{i}", d="", svg="") for i in range(1001)]))
    assert res.source_status == "error" and "1001" in res.error_detail


def test_collector_missing_file_is_error_without_path(tmp_path):
    res = collect(tmp_path / "nao-existe.html")
    assert res.source_status == "error" and str(tmp_path) not in res.error_detail


def test_from_options_requires_a_file_and_registry_has_it():
    with pytest.raises(ValueError, match="import-html"):
        OfflineHtmlCollector.from_options(seed=0, clock=lambda: CAPTURED)
    assert registry.COLLECTORS["offline_html"] is OfflineHtmlCollector
    kinds = [registry.COLLECTORS[n].data_kind for n in ("demo", "downdetector", "offline_html")]
    assert kinds == ["demo", "live", "offline"]


# --- config e migracao ---------------------------------------------------------


def test_db_path_by_data_kind(project_config):
    cfg = project_config
    assert cfg.db_path.name == "bankmon-demo.db" and not cfg.is_offline  # TOML do projeto: demo
    off = dataclasses.replace(cfg, collector="offline_html")
    assert off.db_path.name == "bankmon-offline.db" and off.is_offline and not off.is_demo
    assert dataclasses.replace(cfg, collector="downdetector").db_path.name == "bankmon.db"


def _make_v1(path: Path) -> None:
    schema = db.SCHEMA_PATH.read_text("utf-8").split(db.V2_MARKER)[0] + "PRAGMA user_version = 1;"
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.executescript(
        "INSERT INTO categories VALUES ('c','Cat',1);"
        "INSERT INTO institutions VALUES ('i1','Inst 1','c','Inst 1',1);"
        "INSERT INTO runs (started_at, finished_at, collector, is_demo, trigger, source_status) "
        "VALUES ('2026-10-28T10:00:00Z','2026-10-28T10:00:00Z','demo',1,'cli','ok');"
        "INSERT INTO observations VALUES (1,'i1','attention','x');"
    )
    conn.close()


def test_migration_v1_to_v5_preserves_data(tmp_path):
    path = tmp_path / "v1.db"
    _make_v1(path)
    conn = db.connect(path)
    fresh = db.connect(tmp_path / "fresh.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'run_services'").fetchone()
        db.init_db(conn)
        db.init_db(conn)  # idempotente
        db.init_db(fresh)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_run_sources_sha256'").fetchone()
        assert [tuple(r) for r in conn.execute("SELECT run_id, institution_id, level FROM observations")] == [
            (1, "i1", "attention")]
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM institutions").fetchone()[0] == "Inst 1"
        for table in ("run_sources", "run_services"):
            def cols(c):
                return [tuple(r)[1:3] for r in c.execute(f"PRAGMA table_info({table})")]
            assert cols(conn) and cols(conn) == cols(fresh)
    finally:
        conn.close()
        fresh.close()


def test_init_db_rejects_newer_schema(tmp_path):
    conn = db.connect(tmp_path / "n.db")
    try:
        conn.execute("PRAGMA user_version = 6")
        with pytest.raises(db.DatabaseError):
            db.init_db(conn)
    finally:
        conn.close()


# --- runner: validacao e gravacao ---------------------------------------------


def _record(project_config, services=(), source=None):
    cfg = dataclasses.replace(project_config, collector="offline_html")
    result = CollectionResult("offline_html", False, CAPTURED, CAPTURED, "ok", (), None, tuple(services), source)
    conn = db.open_db(cfg)
    try:
        out = runner.record_result(conn, cfg, result, "cli", clock=lambda: CAPTURED + timedelta(hours=1))
        return out, [dict(r) for r in db.run_services(conn, out.run_id)], db.run_source(conn, out.run_id)
    finally:
        conn.close()


def _svc(**kw):
    base = dict(position=0, service="X", status="normal", status_original="sem problemas", color="blue",
                consistent=True, issue=None)
    return ServiceRecord(**{**base, **kw})


@pytest.mark.parametrize("kw", [
    dict(status="ok"), dict(color="#ff0000"), dict(svg_path_d="M0 0 <script>"), dict(svg_viewbox="0 0 0 0"),
    dict(institution_id="nao-existe"), dict(consistent=1),
])
def test_runner_rejects_invalid_service_as_error_run(project_config, kw):
    out, rows, source = _record(project_config, [_svc(**kw)])
    assert out.result.source_status == "error"
    assert "Resultado inválido" in out.result.error_detail
    assert rows == [] and source is None


def test_runner_sanitizes_texts_and_persists_service_and_source(project_config):
    src = SourceInfo("a.html", "ab" * 32, 10, CAPTURED)
    svc = _svc(service="Foo token=abc123 " + "z" * 500, svg_viewbox="0 0 10 10", svg_path_d="M0 0 L1 1")
    out, rows, source = _record(project_config, [svc], src)
    assert out.result.source_status == "ok"
    assert len(rows) == 1 and len(rows[0]["service"]) <= 200 and "abc123" not in rows[0]["service"]
    assert "stroke" not in rows[0] and rows[0]["svg_path_d"] == "M0 0 L1 1"
    assert source["file_name"] == "a.html" and source["captured_at"] == "2026-10-28T12:00:00Z"
    assert source["imported_at"] == "2026-10-28T13:00:00Z"


def test_runner_rejects_bad_source(project_config):
    out, _, source = _record(project_config, [], SourceInfo("a.html", "nothex", 1, CAPTURED))
    assert out.result.source_status == "error" and source is None


# --- CLI import-html -----------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_import_html_writes_offline_db_only_and_refuses_reimport(project_config, tmp_path, capsys):
    cfg = project_config
    demo = cfg.data_dir / "bankmon-demo.db"
    assert cli.main(["init-db"]) == 0 and demo.exists()
    before = _sha(demo)
    html = write_html(tmp_path)
    args = ["import-html", str(html), "--capturado-em", "2026-10-28T09:00:00-03:00"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "status=ok" in out and "serviços=12" in out
    offline = cfg.data_dir / "bankmon-offline.db"
    assert offline.exists() and not (cfg.data_dir / "bankmon.db").exists()
    assert _sha(demo) == before
    conn = sqlite3.connect(offline)
    try:
        assert conn.execute("SELECT finished_at, collector FROM runs").fetchall() == [
            ("2026-10-28T12:00:00Z", "offline_html")]
        assert conn.execute("SELECT count(*) FROM run_services").fetchone()[0] == 12
        assert conn.execute("SELECT file_name FROM run_sources").fetchone()[0] == "captura.html"
    finally:
        conn.close()
    assert cli.main(args) == 1
    assert "já importado" in capsys.readouterr().err
    copy = tmp_path / "copia.html"  # mesmo conteudo com outro nome tambem e recusado
    copy.write_bytes(html.read_bytes())
    assert cli.main(["import-html", str(copy)]) == 1
    conn = sqlite3.connect(offline)
    try:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()
    assert _sha(demo) == before


def test_import_html_default_capture_is_file_mtime_in_utc(project_config, tmp_path):
    html = write_html(tmp_path)
    os.utime(html, (CAPTURED.timestamp(), CAPTURED.timestamp()))
    assert cli.main(["import-html", str(html)]) == 0
    conn = sqlite3.connect(project_config.data_dir / "bankmon-offline.db")
    try:
        assert conn.execute("SELECT finished_at FROM runs").fetchone()[0] == "2026-10-28T12:00:00Z"
    finally:
        conn.close()


def test_import_html_errors(project_config, tmp_path, capsys):
    assert cli.main(["import-html", str(tmp_path / "nao.html")]) == 1
    html = write_html(tmp_path)
    assert cli.main(["import-html", str(html), "--capturado-em", "2026-10-28T09:00:00"]) == 1  # sem fuso
    assert cli.main(["import-html", str(html), "--capturado-em", "lixo"]) == 1
    capsys.readouterr()
    bad = write_html(tmp_path, FILLERS[:2], "pouco.html")
    assert cli.main(["import-html", str(bad), "--capturado-em", "2026-10-28T12:00:00Z"]) == 1
    assert "incompleta" in capsys.readouterr().out
    assert not (project_config.data_dir / "bankmon.db").exists()


# --- dashboard / status offline ------------------------------------------------


@pytest.fixture
def imported(project_config, tmp_path):
    html = write_html(tmp_path)
    assert cli.main(["import-html", str(html), "--capturado-em", "2026-10-28T12:00:00Z"]) == 0
    return cli.offline_config(project_config)


def get_index(cfg, clock):
    return create_app(cfg, clock=clock).test_client().get("/").get_data(as_text=True)


def test_dashboard_offline_banner_header_and_states(imported):
    html = get_index(imported, lambda: cli.offline_now(imported))
    assert cli.offline_now(imported) == CAPTURED
    assert "DADOS HISTÓRICOS DE VALIDAÇÃO OFFLINE" in html
    assert "capturado em 28/10 09:00" in html and "Não é monitoramento ao vivo" in html
    assert "09:00 (captura histórica)" in html and "<strong>Próxima verificação prevista:</strong> captura histórica" in html
    assert " min)" not in html and "vencida" not in html.lower() and "DEMONSTRAÇÃO" not in html
    assert "|safe" not in html and "<script>" not in html
    states = {name: css for css, name in re.findall(
        r'<article class="card (state-[a-z-]+)".*?<a [^>]*>(.*?)</a>', html, re.S)}
    assert states["Itaú"] == "state-attention" and states["Nubank"] == "state-critical"
    assert states["Bradesco"] == states["Caixa"] == "state-no-alert"
    assert states["Santander"] == states["C6 Bank"] == "state-no-data"
    # so a geometria validada e renderizada (SVG proprio, sem atributos do site)
    assert re.findall(r'<svg class="dd-chart dd-chart-\w+" viewBox="0 0 100 40"', html)
    assert "width=" not in "".join(re.findall(r"<svg.*?</svg>", html, re.S))


def test_dashboard_offline_never_shows_stale_notice(imported):
    html = get_index(imported, lambda: CAPTURED + timedelta(days=3))
    assert "vencida" not in html.lower() and "captura histórica" in html


def test_dashboard_without_offline_has_no_offline_banner(project_config):
    assert "VALIDAÇÃO OFFLINE" not in get_index(project_config, lambda: CAPTURED)


def test_run_services_persisted_with_geometry(imported):
    conn = db.open_readonly(imported.db_path)
    try:
        rid = db.latest_run(conn).run_id
        rows = {r["service"]: r for r in db.run_services(conn, rid)}
        assert len(rows) == 12
        itau = rows["Banco Itaú"]
        assert (itau["svg_viewbox"], itau["svg_width"], itau["svg_height"], itau["svg_path_d"]) == (
            "0 0 100 40", "100", "40", PATH_D)
        assert itau["institution_id"] == "itau" and rows["Servico 0"]["institution_id"] is None
        assert "stroke" not in itau.keys() and itau["color"] == "yellow"
        assert db.source_sha256_exists(conn, db.run_source(conn, rid)["sha256"])
    finally:
        conn.close()


def test_status_offline_prints_historical_capture(imported, capsys):
    assert cli.main(["status", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "OFFLINE" in out and "captura histórica" in out and "28/10/2026 09:00" in out
    assert "Crítico" in out and "vencid" not in out.lower()


def test_status_offline_without_db_fails_cleanly(project_config):
    assert cli.main(["status", "--offline"]) == 1
    assert not (project_config.data_dir / "bankmon-offline.db").exists()


def test_offline_now_falls_back_to_real_clock_without_runs(project_config):
    cfg_off = cli.offline_config(project_config)
    assert abs((cli.offline_now(cfg_off) - datetime.now(timezone.utc)).total_seconds()) < 5
