"""Correcoes da revisao de seguranca (SEC-008..012) e do relogio offline."""

import dataclasses
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bankmon import cli, db, runner
from bankmon.collectors import svg_geometry as g
from bankmon.collectors.base import CollectionResult, SourceInfo
from bankmon.config import ConfigError, load_config
from bankmon.web import create_app
from test_offline_import import CAPTURED, FILLERS, chart, collect, write_html

ISO = "2026-10-28T12:00:00Z"


# --- SEC-008: ASCII estrito e limite de 1e6 --------------------------------------------------


@pytest.mark.parametrize("d", ["M١ 2", "M１ 2", "M0 0 L1e7 1", "M0 0 L-1e7 1", "M0 0 L1 1",
                               "M0 0 L1 1"])
def test_path_d_rejects_non_ascii_and_huge(d):
    assert g.validate_path_d(d) is None


def test_viewbox_and_dimension_reject_non_ascii_and_huge():
    for vb in ("0 0 ١٠ 10", "０ 0 10 10", "-1e7 0 10 10", "0 1e7 10 10", "0 0 10 10 10"):
        assert g.validate_viewbox(vb) is None
    assert g.validate_viewbox("-1e6 1e6 10 10") == "-1e6 1e6 10 10"
    assert g.validate_dimension("٣") is None and g.validate_dimension("１０") is None
    assert g.validate_path_d("M0 0 L1e6 -1e6") == "M0 0 L1e6 -1e6"


# --- SEC-009: valor longo nao vira outro numero ----------------------------------------------


def test_padded_width_and_height_are_rejected_with_issue(tmp_path):
    pad = " " * 195 + "100000"
    bad = chart("Bradesco", svg=f'width="{pad}" height="{pad}"')
    res = collect(write_html(tmp_path, FILLERS + [bad, chart("Y1"), chart("Y2")]))
    svc = next(s for s in res.services if s.service == "Bradesco")
    assert svc.svg_width is None and svc.svg_height is None
    assert "svg_width_invalido" in svc.issue and "svg_height_invalido" in svc.issue


# --- SEC-010: horario de captura -------------------------------------------------------------


@pytest.mark.parametrize("iso", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-05:00",
                                 "1999-12-31T23:59:59Z", "2999-01-01T00:00:00Z"])
def test_capturado_em_extremes_exit_1_without_traceback(project_config, tmp_path, capsys, iso):
    assert cli.main(["import-html", str(write_html(tmp_path)), "--capturado-em", iso]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Erro:") and "Traceback" not in err
    assert not (project_config.data_dir / "bankmon-offline.db").exists()


@pytest.mark.parametrize("mtime", [1e20, -1e20, float("inf")])
def test_absurd_mtime_is_a_clean_error(mtime):
    fake = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=mtime))
    with pytest.raises(ValueError):
        cli._captured_at(fake, None)


# --- relogio offline: so importacao OK -------------------------------------------------------


def test_offline_clock_ignores_failed_runs(project_config, tmp_path):
    assert cli.main(["import-html", str(write_html(tmp_path)), "--capturado-em", ISO]) == 0
    cfg = cli.offline_config(project_config)
    bad = CollectionResult("offline_html", False, CAPTURED, CAPTURED, "bogus")  # invalido -> run `error` "agora"
    conn = db.open_db(cfg)
    try:
        later = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        out = runner.record_result(conn, cfg, bad, "cli", clock=lambda: later)
        assert out.result.source_status == "error" and out.result.finished_at == later
    finally:
        conn.close()
    assert cli.offline_now(cfg) == CAPTURED
    html = create_app(cfg, clock=lambda: cli.offline_now(cfg)).test_client().get("/").get_data(as_text=True)
    assert "vencida" not in html.lower() and "capturado em 28/10 09:00" in html
    assert "state-attention" in html and "state-critical" in html


def test_offline_clock_without_ok_import_uses_real_clock(project_config, tmp_path):
    bad = write_html(tmp_path, FILLERS[:2], "pouco.html")
    assert cli.main(["import-html", str(bad), "--capturado-em", ISO]) == 1  # run `error` gravada
    cfg = cli.offline_config(project_config)
    assert abs((cli.offline_now(cfg) - datetime.now(timezone.utc)).total_seconds()) < 5


# --- SEC-011: file_name sem caminho ----------------------------------------------------------


@pytest.mark.parametrize("name", ["a/b.html", "a\\b.html", "C:x.html", "/etc/passwd", "..\\x.html"])
def test_runner_rejects_file_name_with_path(project_config, name):
    cfg = dataclasses.replace(project_config, collector="offline_html")
    result = CollectionResult("offline_html", False, CAPTURED, CAPTURED, "ok", (), None, (),
                              SourceInfo(name, "ab" * 32, 1, CAPTURED))
    conn = db.open_db(cfg)
    try:
        out = runner.record_result(conn, cfg, result, "cli", clock=lambda: CAPTURED)
        assert out.result.source_status == "error"
        assert db.run_source(conn, out.run_id) is None
    finally:
        conn.close()


# --- SEC-012: offline_html nao pode ser o coletor ativo --------------------------------------


def test_load_config_refuses_offline_html_as_active(write_config):
    write_config(collector="offline_html")
    with pytest.raises(ConfigError, match="import-html"):
        load_config()


# --- IntegrityError ----------------------------------------------------------------------------


@pytest.mark.parametrize("msg, expected", [
    ("UNIQUE constraint failed: run_sources.sha256", "já importado"),
    ("CHECK constraint failed: status", "violação de integridade"),
])
def test_integrity_error_message(project_config, tmp_path, monkeypatch, capsys, msg, expected):
    def boom(*a, **k):
        raise sqlite3.IntegrityError(msg)

    monkeypatch.setattr(runner, "run_collection", boom)
    assert cli.main(["import-html", str(write_html(tmp_path)), "--capturado-em", ISO]) == 1
    err = capsys.readouterr().err
    assert expected in err and ("já importado" in err) == (expected == "já importado")
    assert "Traceback" not in err
