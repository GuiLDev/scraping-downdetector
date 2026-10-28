"""Subcomando `serve`: monta a app e chama waitress.serve com host/porta do config."""

from __future__ import annotations

import pytest

import waitress

from bankmon import cli
from webutil import make_cfg


def test_serve_builds_app_and_calls_waitress(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path, monkeypatch)
    calls = {}

    def fake_serve(app, **kwargs):
        calls["app"], calls["kwargs"] = app, kwargs

    monkeypatch.setattr(waitress, "serve", fake_serve)
    assert cli.main(["serve"]) == cli.EXIT_OK

    assert calls["kwargs"] == {"host": cfg.host, "port": cfg.port, "threads": 4}
    # a app entregue ao waitress e a do dashboard (responde ao test client)
    assert calls["app"].test_client().get("/").status_code == 200
    out = capsys.readouterr().out
    assert f"http://{cfg.host}:{cfg.port}/" in out


def test_serve_has_no_host_option(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--host", "0.0.0.0"])
    assert exc.value.code == 2


def test_serve_ipv6_url_is_bracketed(tmp_path, monkeypatch, capsys):
    make_cfg(tmp_path, monkeypatch)
    toml = tmp_path / "cfg.toml"
    toml.write_text(toml.read_text("utf-8").replace('host = "127.0.0.1"', 'host = "::1"'), "utf-8")
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)
    assert cli.main(["serve"]) == cli.EXIT_OK
    assert "http://[::1]:8765/" in capsys.readouterr().out


def test_serve_rejects_non_loopback_config(tmp_path, monkeypatch, capsys):
    make_cfg(tmp_path, monkeypatch)
    toml = tmp_path / "cfg.toml"
    toml.write_text(toml.read_text("utf-8").replace('host = "127.0.0.1"', 'host = "0.0.0.0"'), "utf-8")
    called = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: called.append(1))
    assert cli.main(["serve"]) == cli.EXIT_CONFIG
    assert not called
