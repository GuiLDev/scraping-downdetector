"""Subcomando `serve`: monta a app e chama waitress.serve com host/porta do config."""

from __future__ import annotations

import pytest

import waitress

from bankmon import cli
from bankmon.web import collect_api
from webutil import make_cfg


def test_serve_builds_app_and_calls_waitress(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path, monkeypatch)
    calls = {}

    def fake_serve(app, **kwargs):
        calls["app"], calls["kwargs"] = app, kwargs

    monkeypatch.setattr(waitress, "serve", fake_serve)
    assert cli.main(["serve"]) == cli.EXIT_OK

    assert calls["kwargs"] == {"host": cfg.host, "port": cfg.port, "threads": 4,
                                "max_request_body_size": collect_api.MAX_BODY_BYTES}
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


# --- Porta ocupada e --abrir ----------------------------------------------------


@pytest.mark.real_port
def test_port_in_use_real_socket():
    import socket

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert cli.port_in_use("127.0.0.1", port) is True
    assert cli.port_in_use("127.0.0.1", port) is False  # fechada


def test_serve_refuses_busy_port(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: called.append(1))
    monkeypatch.setattr(cli, "port_in_use", lambda host, port: (host, port) == (cfg.host, cfg.port))
    assert cli.main(["serve"]) == cli.EXIT_FAIL
    assert not called
    cap = capsys.readouterr()
    assert f"Porta {cfg.port} já está em uso (outro BankMonitor aberto?)" in cap.err
    assert "Dashboard em" not in cap.out


class _SyncTimer:
    def __init__(self, interval, fn, args=()):
        self.interval, self.fn, self.args, self.daemon = interval, fn, args, False

    def start(self):
        self.fn(*self.args)


def test_serve_abrir_opens_only_loopback_url(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)
    monkeypatch.setattr(cli.threading, "Timer", _SyncTimer)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    assert cli.main(["serve", "--abrir"]) == cli.EXIT_OK
    assert opened == [f"http://127.0.0.1:{cfg.port}/"]


def test_serve_without_abrir_does_not_open_browser(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)
    monkeypatch.setattr(cli.threading, "Timer", _SyncTimer)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    assert cli.main(["serve"]) == cli.EXIT_OK
    assert opened == []


def test_serve_abrir_not_called_when_port_busy(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(cli, "port_in_use", lambda host, port: True)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    assert cli.main(["serve", "--abrir"]) == cli.EXIT_FAIL
    assert opened == []
