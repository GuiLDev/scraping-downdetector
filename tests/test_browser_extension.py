"""Coleta pelo navegador, camada 4: CLI `extensao`, `serve --navegador` e o modelo da extensao (TDD)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import waitress

from bankmon import cli, extension
from bankmon.cli import EXIT_FAIL, EXIT_OK, main
from bankmon.config import PROJECT_ROOT, load_config
from browser_helpers import URL, page_html
from webutil import LOCAL_SECTION, make_cfg

HEADER = "X-BankMonitor-Token"


@pytest.fixture
def cfg(write_config):
    from conftest import TOML

    write_config(TOML.format(collector="demo") + LOCAL_SECTION)  # loopback 127.0.0.1:8765, lista explicita
    return load_config()


def run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def generated(cfg: Path | object):
    folder = cfg.data_dir / "extensao-coletor"
    return (
        json.loads((folder / "config.json").read_text("utf-8")),
        json.loads((folder / "manifest.json").read_text("utf-8")),
        folder,
    )


# --- python -m bankmon extensao ---------------------------------------------------------------------


def test_extensao_writes_a_ready_to_load_folder_with_token_and_local_target(cfg, capsys):
    code, out, _ = run(["extensao", "--alvo", "http://127.0.0.1:8777/dd"], capsys)
    assert code == EXIT_OK
    conf, manifest, folder = generated(cfg)
    token = (cfg.data_dir / "coleta-token.txt").read_text("utf-8").strip()
    assert len(token) >= 32 and conf["token"] == token
    assert conf["servidor"] == "http://127.0.0.1:8765"
    assert conf["alvo"].startswith("http://127.0.0.1:") and conf["intervalo_min"] >= 0.5
    assert manifest["manifest_version"] == 3
    assert sorted(manifest["permissions"]) == ["alarms", "scripting", "storage"]
    assert manifest["host_permissions"] == ["http://127.0.0.1:8765/*", "http://127.0.0.1:8777/*"]
    assert (folder / "background.js").is_file()
    assert token not in out  # o terminal nao mostra o token
    assert "Carregar sem pacote" in out or "sem empacotamento" in out


def test_extensao_requires_an_explicit_alvo(cfg, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["extensao"])
    assert exc.value.code == 2 and "--alvo" in capsys.readouterr().err
    assert not (cfg.data_dir / "extensao-coletor").exists()


def test_extensao_options_set_target_and_interval_and_reuse_the_token(cfg, capsys):
    run(["extensao", "--alvo", "http://127.0.0.1:8777/dd"], capsys)
    first = generated(cfg)[0]["token"]
    code, _, _ = run(["extensao", "--alvo", "http://localhost:9100/pagina", "--intervalo", "2.5"], capsys)
    config, manifest, _ = generated(cfg)
    assert code == EXIT_OK and config["token"] == first
    assert (config["alvo"], config["intervalo_min"]) == ("http://localhost:9100/pagina/", 2.5)
    assert manifest["host_permissions"] == ["http://127.0.0.1:8765/*", "http://localhost:9100/*"]


@pytest.mark.parametrize("target", [
    "https://downdetector.com.br/", "http://example.com:8777/", "file:///C:/x.html", "http://127.0.0.1:1/x?q=1",
])
def test_extensao_refuses_unauthorized_target_and_writes_nothing(cfg, capsys, target):
    code, _, err = run(["extensao", "--alvo", target], capsys)
    assert code == EXIT_FAIL and "não autorizado" in err
    assert not (cfg.data_dir / "extensao-coletor").exists()
    assert not (cfg.data_dir / "coleta-token.txt").exists()


@pytest.mark.parametrize("minutes", ["0.1", "0", "-3", "abc"])
def test_extensao_refuses_interval_below_half_minute(cfg, capsys, minutes):
    if minutes == "abc":
        with pytest.raises(SystemExit):
            main(["extensao", "--alvo", "http://127.0.0.1:8777/dd", "--intervalo", minutes])
        return
    code, _, err = run(["extensao", "--alvo", "http://127.0.0.1:8777/dd", "--intervalo", minutes], capsys)
    assert code == EXIT_FAIL and "0,5" in err and not (cfg.data_dir / "extensao-coletor").exists()


def test_extensao_accepts_external_target_only_when_toml_authorizes_it(write_config, capsys, tmp_path, monkeypatch):
    from conftest import TOML

    doc = tmp_path / "doc.md"
    doc.write_text("## Autorizações registradas\n\n- `AUT-9`: fictícia\n", "utf-8")
    monkeypatch.setattr("bankmon.config.AUTHORIZATIONS_DOC", doc)

    extra = '\n[coleta_navegador]\norigens_autorizadas = ["https://exemplo.test"]\nautorizacao_ref = "AUT-9"\n'
    write_config(TOML.format(collector="demo") + extra)
    cfg = load_config()
    code, _, _ = run(["extensao", "--alvo", "https://exemplo.test/status", "--intervalo", "60"], capsys)
    config, manifest, _ = generated(cfg)
    assert code == EXIT_OK and config["alvo"] == "https://exemplo.test/status/"
    assert "https://exemplo.test/*" in manifest["host_permissions"]


def test_generated_token_never_ends_up_in_the_repository(cfg, capsys):
    run(["extensao", "--alvo", "http://127.0.0.1:8777/dd"], capsys)
    token = generated(cfg)[0]["token"]
    assert cfg.data_dir.parts[-1] == "data" and "pytest" in str(cfg.data_dir).lower()
    for root in (PROJECT_ROOT / "extension", PROJECT_ROOT / "src", PROJECT_ROOT / "README.md"):
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        assert files and not any(token in p.read_text("utf-8", errors="ignore") for p in files)
    ignored = (PROJECT_ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert "data/" in ignored  # ali ficam coleta-token.txt e extensao-coletor/
    assert not (PROJECT_ROOT / "extension" / "coletor" / "config.json").exists()


def test_token_file_is_created_once_and_reused(tmp_path):
    a = extension.load_or_create_token(tmp_path)
    assert a == extension.load_or_create_token(tmp_path) == (tmp_path / "coleta-token.txt").read_text("utf-8").strip()
    assert len(a) >= 32 and a != extension.load_or_create_token(tmp_path / "outro")


def test_extensao_without_explicit_origins_refuses(write_config, capsys):
    from conftest import TOML

    write_config(TOML.format(collector="demo"))
    code, _, err = run(["extensao", "--alvo", "http://127.0.0.1:8777/dd"], capsys)
    assert code == EXIT_FAIL and "origens_autorizadas" in err
    assert not (load_config().data_dir / "extensao-coletor").exists()


def test_serve_navegador_without_explicit_origins_is_a_config_error(tmp_path, monkeypatch, capsys):
    make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: pytest.fail("nao deve subir"))
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_CONFIG
    assert "origens_autorizadas" in capsys.readouterr().err


# --- serve --navegador ----------------------------------------------------------------------------------


def test_serve_navegador_enables_the_collection_api_with_the_generated_token(tmp_path, monkeypatch, capsys):
    make_cfg(tmp_path, monkeypatch, extra=LOCAL_SECTION)
    calls = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.update(app=app, kw=kw))
    assert cli.main(["serve", "--navegador"]) == cli.EXIT_OK
    token = (tmp_path / "data" / "coleta-token.txt").read_text("utf-8").strip()
    client = calls["app"].test_client()
    html = page_html()
    reserva = client.post("/api/coleta/reserva", json={"alvo": URL}, headers={HEADER: token}).get_json()
    ok = client.post("/api/coleta", json={"url": URL, "html": html, "reserva_id": reserva["reserva_id"]},
                     headers={HEADER: token})
    assert ok.status_code == 200 and ok.get_json()["page_state"] == "ok"
    assert client.post("/api/coleta", json={"url": URL, "html": html}).status_code == 401
    assert (tmp_path / "data" / "bankmon-navegador.db").exists()
    out = capsys.readouterr().out
    assert "COLETA PELO NAVEGADOR" in out and token not in out
    assert calls["kw"]["host"] == "127.0.0.1"


def test_serve_without_navegador_has_no_collection_routes(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch)
    calls = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.update(app=app))
    cli.main(["serve"])
    assert calls["app"].test_client().post("/api/coleta", json={}).status_code == 404


def test_serve_navegador_and_offline_are_mutually_exclusive(tmp_path, monkeypatch):
    make_cfg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--navegador", "--offline"])
    assert exc.value.code == 2


# --- modelo versionado da extensao (politica: sem cookies, sem interagir com desafios) ----------------------


EXT = PROJECT_ROOT / "extension" / "coletor"


def test_extension_template_declares_only_the_required_permissions():
    manifest = json.loads((EXT / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 3
    assert sorted(manifest["permissions"]) == ["alarms", "scripting", "storage"]
    assert manifest["background"]["service_worker"] == "background.js"
    assert all(re.fullmatch(r"https?://(127\.0\.0\.1|localhost)(:\d+)?/\*", h) for h in manifest["host_permissions"])


def test_extension_script_respects_the_prohibitions():
    code = (EXT / "background.js").read_text("utf-8")
    # os comentarios explicam as proibicoes: so o codigo executavel e checado
    no_line_comments = re.sub(r"(?m)^\s*//.*$", "", code)
    code_only = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.S)
    for forbidden in ("cookies", "document.cookie", "localStorage", "sessionStorage", "tabs.create",
                      "tabs.update", "windows.", "active: true", "highlight", ".click(", "dispatchEvent",
                      "setRequestHeader", "webRequest", "debugger"):
        assert forbidden not in code_only, forbidden
    assert "chrome.tabs.reload" in code_only and "coletarAgora" in code_only
    assert "// " in code and "cookies" in code  # as proibicoes estao documentadas em comentarios
