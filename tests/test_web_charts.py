"""Graficos de 24 h nos cartoes (geometria gravada, revalidada) e horario "Observado em"."""

from __future__ import annotations

import dataclasses
import re
import sqlite3

import pytest

from bankmon import db, runner
from bankmon.collectors.base import CollectionResult, Observation, ServiceRecord
from webutil import MIN, NOW, add_run, cards, client_for, make_cfg, page

CAPTURED = NOW - 10 * MIN  # 12:20 em Sao Paulo
VIEWBOX = "0 0 243 40"
D = {"alfa": "M0 30 L10 5 L20 28", "beta": "M0 20 L50 22.5 L100 10", "gama": "M0 39 L243 39"}
LEVELS = {"alfa": "critical", "beta": "attention", "gama": "no_alert", "delta": "not_listed"}
SPEC = {"alfa": ("red", "critical"), "beta": ("yellow", "warning"), "gama": ("blue", "normal")}
SVG_RE = re.compile(r"<svg.*?</svg>", re.S)


def _services(**override):
    out = []
    for pos, (iid, (color, st)) in enumerate(SPEC.items()):
        kw = dict(position=pos, service=iid, status=st, status_original=None, color=color,
                  consistent=True, issue=None, institution_id=iid, svg_viewbox=VIEWBOX,
                  svg_width="243", svg_height="40", svg_path_d=D[iid])
        kw.update(override.get(iid, {}))
        out.append(ServiceRecord(**kw))
    return out


def offline_cfg(tmp_path, monkeypatch, **override):
    cfg = dataclasses.replace(make_cfg(tmp_path, monkeypatch), collector="offline_html")
    assert cfg.is_offline
    result = CollectionResult(
        "offline_html", False, CAPTURED, CAPTURED, "ok",
        tuple(Observation(i, lvl, "d") for i, lvl in LEVELS.items() if lvl != "not_listed"),
        None, tuple(_services(**override)),
    )
    conn = db.open_db(cfg)
    try:
        out = runner.record_result(conn, cfg, result, "cli", clock=lambda: CAPTURED)
        assert out.result.source_status == "ok"
    finally:
        conn.close()
    return cfg


def card_of(html, name):
    return next(c for c in cards(html) if f">{name}</a>" in c)


def force(cfg, sql, *params):
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_charts_rendered_with_stored_geometry(tmp_path, monkeypatch):
    cfg = offline_cfg(tmp_path, monkeypatch)
    html = page(client_for(cfg, CAPTURED))
    for name, iid, color in (("Banco Alfa", "alfa", "red"), ("Banco Beta", "beta", "yellow"),
                             ("Banco Gama", "gama", "blue")):
        card = card_of(html, name)
        m = re.search(r'<svg class="dd-chart dd-chart-(\w+)" viewBox="([^"]*)"[^>]*><path d="([^"]*)"/></svg>', card)
        assert m, card
        assert (m[1], m[2], m[3]) == (color, VIEWBOX, D[iid])
        assert 'preserveAspectRatio="none"' in card and 'role="img"' in card
        assert f"Relatos de {name} nas últimas 24 horas segundo o Downdetector" in card
    assert "dd-chart" not in card_of(html, "Banco Delta")  # not_listed


def test_states_and_counts_unchanged_by_charts(tmp_path, monkeypatch):
    cfg = offline_cfg(tmp_path, monkeypatch)
    off = page(client_for(cfg, CAPTURED))
    from test_web_extra import count_items, section_counts
    assert count_items(off) == {"Crítico": 1, "Atenção": 1, "Sem alerta": 1, "Sem dados": 1, "Não monitorado": 0}
    assert section_counts(off) == [2, 1, 1]
    names = re.findall(r'<h4 class="card-name"><a [^>]*>(.*?)</a>', off)
    assert names == ["Banco Alfa", "Banco Beta", "Banco Gama", "Banco Delta"]


@pytest.mark.parametrize("override", [
    {"consistent": False}, {"color": "other"}, {"svg_path_d": None}, {"svg_viewbox": None},
])
def test_no_chart_when_service_not_eligible(tmp_path, monkeypatch, override):
    cfg = offline_cfg(tmp_path, monkeypatch, beta=override)
    html = page(client_for(cfg, CAPTURED))
    assert "dd-chart" not in card_of(html, "Banco Beta")
    assert "dd-chart-red" in card_of(html, "Banco Alfa")


@pytest.mark.parametrize("col,value", [
    ("svg_path_d", "M0 0 <script>alert(1)</script>"),
    ("svg_path_d", 'M0 0" onload="x'),
    ("svg_viewbox", '0 0 243 40" onload="x'),
    ("svg_viewbox", "0 0 0 0"),
    ("svg_width", "<b>"),
])
def test_forced_invalid_geometry_gives_no_chart_and_page_ok(tmp_path, monkeypatch, col, value):
    cfg = offline_cfg(tmp_path, monkeypatch)
    force(cfg, f"UPDATE run_services SET {col} = ? WHERE institution_id = 'beta'", value)
    html = page(client_for(cfg, CAPTURED))
    assert "dd-chart" not in card_of(html, "Banco Beta")
    assert "onload" not in html and "alert(1)" not in html
    assert "dd-chart-yellow" not in html and "dd-chart-red" in html


def test_demo_has_no_charts_and_keeps_desde(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    add_run(cfg, NOW - 10 * MIN, LEVELS)
    html = page(client_for(cfg))
    assert "dd-chart" not in html and "<svg" not in html
    assert "Observado em" not in html
    assert re.search(r"desde \d\d:\d\d", html)


def test_offline_shows_observed_at_instead_of_desde(tmp_path, monkeypatch):
    cfg = offline_cfg(tmp_path, monkeypatch)
    html = page(client_for(cfg, CAPTURED))
    assert "Observado em: 28/10 às 12:20" in card_of(html, "Banco Alfa")
    assert "desde" not in "".join(cards(html))


def test_chart_svgs_are_inert_and_headers_intact(tmp_path, monkeypatch):
    cfg = offline_cfg(tmp_path, monkeypatch)
    resp = client_for(cfg, CAPTURED).get("/")
    html = resp.get_data(as_text=True)
    svgs = SVG_RE.findall(html)
    assert len(svgs) == 3
    for svg in svgs:
        for bad in ("<script", "javascript:", "url(", "href=", "style="):
            assert bad not in svg.lower()
    assert "var(--color-dd" not in html and "stroke=" not in html
    csp = resp.headers["Content-Security-Policy"]
    assert "style-src 'self'" in csp and "'unsafe-inline'" not in csp
    for h in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert h in resp.headers
