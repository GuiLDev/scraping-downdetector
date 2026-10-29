"""Helpers dos testes do dashboard (independentes do conftest compartilhado)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from bankmon import db, runner
from bankmon.collectors.base import CollectionResult, Observation
from bankmon.config import Config, load_config
from bankmon.web import create_app

NOW = datetime(2026, 10, 28, 15, 30, 0, tzinfo=timezone.utc)  # 12:30 em Sao Paulo
MIN = timedelta(minutes=1)

INSTITUTIONS = (
    ("alfa", "Banco Alfa", "tradicionais"),
    ("beta", "Banco Beta", "tradicionais"),
    ("gama", "Banco Gama", "digitais"),
    ("delta", "Banco Delta", "digitais"),
)


LOCAL_ORIGIN = "http://127.0.0.1:8777"
LOCAL_SECTION = (
    f'\n[coleta_navegador]\norigens_autorizadas = ["{LOCAL_ORIGIN}", "http://localhost:9100"]\n'
    "intervalo_minimo_local_s = 0\n"
)


def toml_text(collector: str = "demo", institutions=INSTITUTIONS, extra: str = "") -> str:
    parts = [
        f'[collector]\nactive = "{collector}"\n',
        "[schedule]\ninterval_minutes = 60\nstale_after_minutes = 90\n",
        '[server]\nhost = "127.0.0.1"\nport = 8765\n',
        "[demo]\nseed = 7\n",
        '[[categories]]\nid = "tradicionais"\nname = "Bancos tradicionais"\norder = 1\n',
        '[[categories]]\nid = "digitais"\nname = "Bancos digitais"\norder = 2\n',
    ]
    for iid, name, cat in institutions:
        parts.append(
            f'[[institutions]]\nid = "{iid}"\nname = {json.dumps(name)}\n'
            f'category = "{cat}"\ndd_label = {json.dumps(name)}\n'
        )
    return "\n".join(parts) + extra


def make_cfg(tmp_path, monkeypatch, collector="demo", institutions=INSTITUTIONS, extra: str = "") -> Config:
    path = tmp_path / "cfg.toml"
    path.write_text(toml_text(collector, institutions, extra), "utf-8")
    monkeypatch.setenv("BANKMON_CONFIG", str(path))
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def add_run(cfg: Config, at: datetime, levels=None, status="ok", error=None) -> int:
    """Grava uma execucao (e transicoes) via runner.record_result, com relogio em `at`."""
    result = CollectionResult(
        cfg.collector,
        cfg.is_demo,
        at,
        at,
        status,
        tuple(Observation(i, lvl, "detalhe") for i, lvl in (levels or {}).items()),
        error,
    )
    conn = db.open_db(cfg)
    try:
        return runner.record_result(conn, cfg, result, "cli", clock=lambda: at).run_id
    finally:
        conn.close()


def client_for(cfg: Config, now: datetime = NOW):
    return create_app(cfg, clock=lambda: now).test_client()


def page(client, url: str = "/", **kw) -> str:
    resp = client.get(url, **kw)
    assert resp.status_code == 200, (url, resp.status_code)
    return resp.get_data(as_text=True)


def cards(html: str) -> list[str]:
    """HTML de cada cartao de instituicao (<article class="card ...">)."""
    return re.findall(r'<article class="card .*?</article>', html, re.S)


def json_block(html: str, element_id: str):
    m = re.search(rf'<script type="application/json" id="{element_id}">(.*?)</script>', html, re.S)
    assert m, f"bloco JSON {element_id} ausente"
    return json.loads(m.group(1))
