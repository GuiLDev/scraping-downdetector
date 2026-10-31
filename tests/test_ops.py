"""E1.3: scripts de agendamento, units do systemd, README e coletas agendadas em processo."""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bankmon import runner, timeutil
from bankmon.cli import EXIT_LOCKED, EXIT_OK, main
from bankmon.config import load_config

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "scripts" / "windows"
LINUX = ROOT / "scripts" / "linux"

POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="requer Windows PowerShell")


def _ps(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def _task_names() -> list[str]:
    res = _ps("-Command", "(Get-ScheduledTask -TaskPath '\\' -TaskName 'BankMonitor-*' "
              "-ErrorAction SilentlyContinue | Sort-Object TaskName).TaskName -join ','")
    return res.stdout.strip().split(",") if res.stdout.strip() else []


# --- Windows ------------------------------------------------------------------


@needs_powershell
@pytest.mark.parametrize("script", ["register-tasks.ps1", "unregister-tasks.ps1"])
def test_ps1_parses_without_errors(script):
    cmd = (
        "$e=$null;$t=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{WIN / script}',[ref]$t,[ref]$e);"
        "if($e.Count){$e|%{$_.Message};exit 1}"
    )
    res = _ps("-Command", cmd)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("script", ["register-tasks.ps1", "unregister-tasks.ps1"])
def test_ps1_is_ascii(script):
    # Windows PowerShell 5.1 le .ps1 sem BOM como ANSI: ASCII evita acentos corrompidos.
    (WIN / script).read_bytes().decode("ascii")


@needs_powershell
def test_register_dryrun_prints_both_tasks_and_registers_nothing(tmp_path):
    fake = tmp_path / "proj"
    (fake / ".venv" / "Scripts").mkdir(parents=True)
    (fake / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    (fake / "config").mkdir()
    (fake / "config" / "institutions.toml").write_text("", "utf-8")

    before = _task_names()
    res = _ps("-File", str(WIN / "register-tasks.ps1"), "-ProjectRoot", str(fake), "-DryRun")
    out = res.stdout
    assert res.returncode == 0, out + res.stderr
    assert "BankMonitor-Collect" in out and "BankMonitor-Dashboard" in out
    assert "-P -m bankmon collect --trigger scheduled" in out and "-P -m bankmon serve" in out
    assert "IgnoreNew" in out and "30 min" in out
    assert str(fake) in out  # WorkingDirectory
    assert "Nada foi registrado" in out

    assert _task_names() == before  # a lista de tarefas nao muda (independe de registros legitimos)


@needs_powershell
def test_register_fails_without_venv(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "institutions.toml").write_text("", "utf-8")
    res = _ps("-File", str(WIN / "register-tasks.ps1"), "-ProjectRoot", str(tmp_path), "-DryRun")
    assert res.returncode != 0
    assert "Ambiente virtual" in res.stdout + res.stderr


@needs_powershell
def test_unregister_dryrun_removes_nothing():
    res = _ps("-File", str(WIN / "unregister-tasks.ps1"), "-DryRun")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "BankMonitor-Collect" in res.stdout and "BankMonitor-Dashboard" in res.stdout
    assert "Nada foi removido" in res.stdout


def test_start_dashboard_cmd_runs_serve_from_root():
    text = (WIN / "start-dashboard.cmd").read_text("utf-8")
    assert r'.venv\Scripts\python.exe" -P -m bankmon serve' in text
    assert r"%~dp0..\.." in text


def test_iniciar_leitura_pontual_cmd():
    raw = (WIN / "iniciar-leitura-pontual.cmd").read_bytes()
    raw.decode("ascii")  # sem acentos: nao depende de codepage
    text = raw.decode("ascii")
    assert r"%~dp0..\.." in text
    assert r'.venv\Scripts\python.exe" -P -m bankmon serve --navegador --abrir' in text
    assert r'set "BANKMON_CONFIG=%CD%\data\leitura-pontual\leitura.toml"' in text
    assert r'set "BANKMON_DATA_DIR=%CD%\data\leitura-pontual\data"' in text
    assert "SEC-002" in text
    for line in text.splitlines():
        if line.strip().lower().startswith("start "):
            assert "downdetector" not in line.lower()
    assert "coleta-token" not in text and "extensao --" not in text


# --- Linux (systemd --user) ---------------------------------------------------


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in (LINUX / name).read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
        elif "=" in line and current is not None:
            key, value = line.split("=", 1)
            current.setdefault(key, []).append(value)
    return sections


@pytest.mark.parametrize(
    "name, command, kind",
    [
        ("bankmon-collect.service", "collect --trigger scheduled", "oneshot"),
        ("bankmon-serve.service", "serve", "simple"),
    ],
)
def test_service_units(name, command, kind):
    service = _unit(name)["Service"]
    assert service["Type"] == [kind]
    assert service["WorkingDirectory"] == ["%h/bankmon"]
    exec_start = service["ExecStart"][0]
    assert exec_start.startswith("%h/bankmon/.venv/bin/python -P -m bankmon ")
    assert exec_start.endswith(command)
    assert service["NoNewPrivileges"] == ["yes"]
    assert service["PrivateTmp"] == ["yes"]
    assert service["ProtectSystem"] == ["strict"]
    assert any("%h/bankmon/data" in p for p in service["ReadWritePaths"])


def test_serve_service_restarts_on_failure():
    unit = _unit("bankmon-serve.service")
    assert unit["Service"]["Restart"] == ["on-failure"]
    assert unit["Install"]["WantedBy"] == ["default.target"]


def test_collect_timer_is_hourly_and_persistent():
    unit = _unit("bankmon-collect.timer")
    assert unit["Timer"]["OnCalendar"] == ["hourly"]
    assert unit["Timer"]["Persistent"] == ["true"]
    assert unit["Timer"]["Unit"] == ["bankmon-collect.service"]
    assert re.fullmatch(r"\d+(s|sec)?", unit["Timer"]["RandomizedDelaySec"][0])
    assert unit["Install"]["WantedBy"] == ["timers.target"]


# --- README -------------------------------------------------------------------


def test_readme_documents_commands_and_disclaimer():
    text = (ROOT / "README.md").read_text("utf-8")
    for needle in (
        "init-db", "check-config", "seed-demo", "serve", "collect", "status", "backup",
        "register-tasks.ps1", "unregister-tasks.ps1", "enable-linger", "BANKMON_DATA_DIR",
        "BANKMON_CONFIG", "docs/etapa2-viabilidade-coletor.md", "pytest",
    ):
        assert needle in text, needle
    assert "não é confirmação oficial" in text or "não são confirmação oficial" in text
    assert "loopback" in text and "Content-Security-Policy" in text
    assert "Repo dedicado para scraping" not in text


# --- Coletas agendadas em processo --------------------------------------------


@pytest.fixture
def scheduled_env(project_config, monkeypatch):
    # O relogio do runner e o do coletor demo devem coincidir (a conftest fixa o do runner).
    monkeypatch.setattr(runner, "utc_now", timeutil.utc_now)
    assert main(["init-db"]) == EXIT_OK
    return project_config


def test_three_consecutive_scheduled_collections_are_recorded(scheduled_env, capsys):
    for _ in range(3):
        assert main(["collect", "--trigger", "scheduled"]) in (EXIT_OK, 1)  # demo pode simular falha
    conn = sqlite3.connect(scheduled_env.db_path)
    try:
        rows = conn.execute("SELECT trigger, is_demo, source_status FROM runs ORDER BY id").fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {"scheduled"}
    assert {r[1] for r in rows} == {1}


def test_overlapping_collection_is_blocked_by_lock(scheduled_env, capsys):
    with runner.file_lock(scheduled_env.lock_path):
        assert main(["collect", "--trigger", "scheduled"]) == EXIT_LOCKED
    conn = sqlite3.connect(scheduled_env.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        conn.close()
    # Liberado o lock, a coleta seguinte funciona.
    assert main(["collect", "--trigger", "scheduled"]) in (EXIT_OK, 1)


def test_setup_logging_without_console_uses_only_file(tmp_path, monkeypatch):
    """pythonw.exe: sys.stderr e None -> nenhum StreamHandler, so o arquivo de log."""
    import logging
    import sys
    from types import SimpleNamespace

    from bankmon import cli

    monkeypatch.setattr(sys, "stderr", None)
    try:
        cli.setup_logging(SimpleNamespace(log_dir=tmp_path / "logs"))
        handlers = logging.getLogger("bankmon").handlers
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.FileHandler)
    finally:
        cli.close_logging()


# --- SEC-012/013/015 ----------------------------------------------------------


def test_python_P_flag_in_every_execution_point():
    """-P impede que a raiz do projeto (cwd) em sys.path[0] sobreponha a stdlib."""
    assert "'-P -m bankmon collect --trigger scheduled'" in (WIN / "register-tasks.ps1").read_text("ascii")
    assert "'-P -m bankmon serve'" in (WIN / "register-tasks.ps1").read_text("ascii")
    assert 'python.exe" -P -m bankmon serve' in (WIN / "start-dashboard.cmd").read_text("utf-8")
    assert 'python.exe" -P -m bankmon serve --navegador --abrir' in (
        WIN / "iniciar-leitura-pontual.cmd"
    ).read_text("utf-8")
    for name in ("bankmon-collect.service", "bankmon-serve.service"):
        assert " -P -m bankmon " in _unit(name)["Service"]["ExecStart"][0]
    docs = (ROOT / "README.md").read_text("utf-8") + (ROOT / "docs" / "operacao.md").read_text("utf-8")
    assert not re.search(r"python(w)?(\.exe)? -m bankmon", docs)


def test_systemd_hardening_directives():
    for name in ("bankmon-collect.service", "bankmon-serve.service"):
        svc = _unit(name)["Service"]
        assert svc["PrivateUsers"] == ["yes"]
        assert svc["UMask"] == ["0077"]
        assert svc["SystemCallFilter"] == ["@system-service"]
    assert _unit("bankmon-collect.service")["Service"]["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert _unit("bankmon-serve.service")["Service"]["RestrictAddressFamilies"] == [
        "AF_UNIX AF_INET AF_INET6"
    ]
    serve_unit = _unit("bankmon-serve.service")["Unit"]
    assert serve_unit["StartLimitIntervalSec"] == ["600"]
    assert serve_unit["StartLimitBurst"] == ["5"]


def test_windows_scripts_hardening_text():
    reg = (WIN / "register-tasks.ps1").read_text("ascii")
    assert "-AllowOutsideProfile" in reg and "$env:USERPROFILE" in reg
    assert reg.count("Register-ScheduledTask -TaskPath '" + chr(92) + "'") == 2
    assert '-Execute "`"$pythonw`""' in reg
    assert "-TaskPath '" + chr(92) + "'" in (WIN / "unregister-tasks.ps1").read_text("ascii")
    cmd = (WIN / "start-dashboard.cmd").read_text("utf-8")
    assert 'em "%CD%\\.venv"' in cmd


@needs_powershell
def test_register_refuses_project_outside_profile():
    outside = Path(Path.home().anchor) / "bankmon-nonexistent-test"
    # Raiz do disco existe; usa um diretorio real fora do perfil (Windows) sem criar nada.
    root = Path(sys.executable).anchor + "Windows"
    res = _ps("-File", str(WIN / "register-tasks.ps1"), "-ProjectRoot", root, "-DryRun")
    assert res.returncode != 0
    assert "fora de" in res.stdout + res.stderr
    assert not outside.exists()


def test_lock_includes_build_tools_with_hashes():
    lock = (ROOT / "requirements.lock").read_text("utf-8")
    assert re.search(r"^setuptools==\S+ \\\s+--hash=sha256:", lock, re.M)
    assert re.search(r"^pytest==\S+ \\", lock, re.M)
    assert "--no-build-isolation" in (ROOT / "README.md").read_text("utf-8")
    assert "pip install -e .[dev]" not in (ROOT / "README.md").read_text("utf-8")


@needs_powershell
def test_register_dryrun_default_root_via_file():
    """PS 5.1 + -File: $PSScriptRoot em default de parametro vem vazio; o padrao deve funcionar."""
    res = _ps("-File", str(WIN / "register-tasks.ps1"), "-DryRun")
    assert res.returncode == 0, res.stdout + res.stderr
    assert str(ROOT) in res.stdout
