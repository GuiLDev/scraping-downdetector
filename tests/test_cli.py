"""CLI em processo: `bankmon.cli.main([...])` com BANKMON_DATA_DIR/BANKMON_CONFIG em tmp_path."""

import os
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

from bankmon import cli, runner
from bankmon.cli import DEMO_WARNING, EXIT_CONFIG, EXIT_FAIL, EXIT_LOCKED, EXIT_OK, main
from bankmon.collectors.downdetector import PENDING_MESSAGE
from bankmon.config import DEFAULT_CONFIG_PATH, load_config
from conftest import T0, TOML, assert_lock_free, db_rows, find_ok_moment

MIN = timedelta(minutes=1)


@pytest.fixture
def clock(monkeypatch):
    """Congela o relogio da CLI e do runner e devolve um controlador."""

    class Clock:
        now = T0

        def set(self, moment):
            Clock.now = moment

    monkeypatch.setattr(runner, "utc_now", lambda: Clock.now)
    monkeypatch.setattr(cli, "utc_now", lambda: Clock.now)
    return Clock()


@pytest.fixture
def demo_env(write_config, clock):
    write_config()
    cfg = load_config()
    clock.set(find_ok_moment(cfg))
    return cfg


@pytest.fixture
def real_env(write_config, clock):
    write_config(collector="downdetector")
    return load_config()


def run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_data_dir_is_isolated_from_project(demo_env):
    assert "data" in demo_env.data_dir.parts
    project_data = DEFAULT_CONFIG_PATH.parents[1] / "data"
    assert demo_env.data_dir != project_data


# --- init-db -----------------------------------------------------------------------------------


def test_init_db_creates_database_and_syncs(demo_env, capsys):
    code, out, _ = run(["init-db"], capsys)
    assert code == EXIT_OK
    assert str(demo_env.db_path) in out
    assert demo_env.db_path.name == "bankmon-demo.db" and demo_env.db_path.exists()
    assert db_rows(demo_env, "SELECT COUNT(*) FROM institutions") == [(2,)]
    assert db_rows(demo_env, "SELECT COUNT(*) FROM runs") == [(0,)]


def test_init_db_twice_is_ok(demo_env, capsys):
    assert run(["init-db"], capsys)[0] == EXIT_OK
    assert run(["init-db"], capsys)[0] == EXIT_OK
    assert db_rows(demo_env, "SELECT COUNT(*) FROM institutions") == [(2,)]


def test_init_db_incompatible_version_exits_1(demo_env, capsys):
    run(["init-db"], capsys)
    raw = sqlite3.connect(demo_env.db_path)
    raw.execute("PRAGMA user_version = 42")
    raw.close()
    code, _, err = run(["init-db"], capsys)
    assert code == EXIT_FAIL and "Erro de banco" in err


# --- check-config ------------------------------------------------------------------------------


def test_check_config_ok_prints_summary_without_touching_data_dir(demo_env, capsys):
    code, out, _ = run(["check-config"], capsys)
    assert code == EXIT_OK
    assert "Configuração OK" in out and "demonstração" in out
    assert "60 min" in out and "90 min" in out
    assert not demo_env.data_dir.exists()  # check-config nao cria banco nem logs


def test_check_config_on_project_toml(monkeypatch, tmp_path, capsys):
    """E1.0: `check-config` valida o TOML de exemplo do repositorio."""
    monkeypatch.delenv("BANKMON_CONFIG", raising=False)
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "d"))
    code, out, _ = run(["check-config"], capsys)
    assert code == EXIT_OK and "15 (15 habilitadas)" in out


@pytest.mark.parametrize(
    "old,new,fragment",
    [
        ('id = "inst-2"', 'id = "inst-1"', "duplicada"),
        ('category = "a"', 'category = "inexistente"', "categoria inexistente"),
        ('active = "demo"', 'active = "xyz"', "coletor desconhecido"),
        ("stale_after_minutes = 90", "stale_after_minutes = 30", "stale_after_minutes"),
        ('"127.0.0.1"', '"0.0.0.0"', "server.host"),
    ],
)
@pytest.mark.parametrize("command", ["check-config", "init-db", "collect", "status", "backup"])
def test_invalid_config_exits_3_with_clear_message(write_config, capsys, old, new, fragment, command):
    write_config(TOML.format(collector="demo").replace(old, new, 1))
    code, out, err = run([command], capsys)
    assert code == EXIT_CONFIG == 3
    assert "Erro de configuração" in err and fragment in err
    assert out == ""


def test_missing_config_file_exits_3(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BANKMON_CONFIG", str(tmp_path / "nao-existe.toml"))
    monkeypatch.setenv("BANKMON_DATA_DIR", str(tmp_path / "d"))
    code, _, err = run(["check-config"], capsys)
    assert code == 3 and "não encontrado" in err
    assert not (tmp_path / "d").exists()


def test_malformed_toml_tables_should_exit_3(write_config, capsys):
    text = TOML.format(collector="demo")
    write_config("institutions = [1]\n" + text[: text.index("[[institutions]]")])
    assert main(["check-config"]) == EXIT_CONFIG


# --- collect -------------------------------------------------------------------------------------


def test_collect_demo_ok_exits_0_and_records_demo_run(demo_env, capsys):
    code, out, _ = run(["collect"], capsys)
    assert code == EXIT_OK and "status=ok" in out
    assert db_rows(demo_env, "SELECT source_status, is_demo, trigger, collector FROM runs") == [
        ("ok", 1, "cli", "demo")
    ]
    assert db_rows(demo_env, "SELECT COUNT(*) FROM observations") == [(2,)]
    assert demo_env.data_dir.joinpath("logs", "bankmon.log").exists()
    assert_lock_free(demo_env.lock_path)


def test_collect_scheduled_trigger_is_recorded(demo_env, capsys):
    assert run(["collect", "--trigger", "scheduled"], capsys)[0] == EXIT_OK
    assert db_rows(demo_env, "SELECT trigger FROM runs") == [("scheduled",)]


def test_collect_rejects_invalid_trigger(demo_env):
    with pytest.raises(SystemExit) as exc:
        main(["collect", "--trigger", "manual"])
    assert exc.value.code == 2


def test_collect_with_downdetector_stub_exits_1_and_records_not_authorized(real_env, capsys):
    code, out, _ = run(["collect"], capsys)
    assert code == EXIT_FAIL == 1
    assert "not_authorized" in out and PENDING_MESSAGE in out
    assert real_env.db_path.name == "bankmon.db"
    assert db_rows(real_env, "SELECT source_status, is_demo, error_detail FROM runs") == [
        ("not_authorized", 0, PENDING_MESSAGE)
    ]
    assert db_rows(real_env, "SELECT COUNT(*) FROM observations") == [(0,)]
    assert not (real_env.data_dir / "bankmon-demo.db").exists()


def test_collect_with_busy_lock_exits_2_and_writes_nothing(demo_env, capsys):
    with runner.file_lock(demo_env.lock_path):
        code, out, _ = run(["collect"], capsys)
    assert code == EXIT_LOCKED == 2
    assert not demo_env.db_path.exists()
    # liberado o lock, a coleta seguinte funciona
    assert run(["collect"], capsys)[0] == EXIT_OK


def test_collect_with_leftover_unheld_lock_file_runs(demo_env, capsys):
    # arquivo de lock sobrando (processo anterior morreu): o SO ja o liberou
    demo_env.lock_path.parent.mkdir(parents=True, exist_ok=True)
    demo_env.lock_path.write_text("999")
    assert run(["collect"], capsys)[0] == EXIT_OK
    assert_lock_free(demo_env.lock_path)


def test_collect_with_collector_exception_exits_1(demo_env, capsys, monkeypatch):
    from bankmon.collectors.demo import DemoCollector

    def boom(self, institutions):
        raise ConnectionError("rede caiu")

    monkeypatch.setattr(DemoCollector, "collect", boom)
    code, out, _ = run(["collect"], capsys)
    assert code == EXIT_FAIL and "ConnectionError" in out
    assert db_rows(demo_env, "SELECT source_status FROM runs") == [("error",)]


# --- status ------------------------------------------------------------------------------------


def test_status_demo_shows_demo_warning_even_without_runs(demo_env, capsys):
    code, out, _ = run(["status"], capsys)
    assert code == EXIT_OK
    assert "DADOS DE DEMONSTRAÇÃO" in out and DEMO_WARNING in out
    assert "Última verificação válida: nenhuma" in out
    assert "== Com alerta (0) ==" in out and "== Sem alerta identificado (0) ==" in out
    assert "== Sem cobertura (2) ==" in out  # nunca verde sem execucao valida
    assert "Sem alerta " not in out.replace("Sem alerta identificado", "")
    assert "Indicador do Downdetector" in out


def test_status_after_collect_shows_last_valid_check(demo_env, clock, capsys):
    run(["collect"], capsys)
    clock.set(clock.now + 12 * MIN)
    code, out, _ = run(["status"], capsys)
    assert code == EXIT_OK and "DADOS DE DEMONSTRAÇÃO" in out
    assert "(há 12 min)" in out
    assert "Fonte:" not in out
    assert "== Sem cobertura (0) ==" in out


def test_status_deadline_90_vs_91_minutes(demo_env, clock, capsys):
    run(["collect"], capsys)
    ran_at = clock.now
    clock.set(ran_at + 90 * MIN)
    _, out90, _ = run(["status"], capsys)
    assert "== Sem cobertura (0) ==" in out90
    clock.set(ran_at + 91 * MIN)
    _, out91, _ = run(["status"], capsys)
    assert "== Sem cobertura (2) ==" in out91
    assert "== Com alerta (0) ==" in out91 and "== Sem alerta identificado (0) ==" in out91


def test_status_after_failed_latest_run_everything_no_data_and_source_notice(demo_env, clock, capsys):
    run(["collect"], capsys)
    ok_moment = clock.now
    # nova execucao com falha 5 min depois (coletor injetado direto no runner)
    from conftest import FakeCollector

    runner.run_collection(
        demo_env, "cli", now=ok_moment + 5 * MIN,
        collector=FakeCollector(status="inaccessible", at=ok_moment + 5 * MIN, is_demo=True),
    )
    clock.set(ok_moment + 6 * MIN)
    _, out, _ = run(["status"], capsys)
    assert "== Sem cobertura (2) ==" in out
    assert "Fonte: inaccessible" in out
    assert "Última verificação válida:" in out and "(há 6 min)" in out


def test_status_real_mode_has_no_demo_warning_and_shows_pending_authorization(real_env, capsys):
    run(["collect"], capsys)
    code, out, _ = run(["status"], capsys)
    assert code == EXIT_OK
    assert "DEMONSTRAÇÃO" not in out
    assert "not_authorized" in out and PENDING_MESSAGE in out
    assert "== Sem cobertura (2) ==" in out
    assert "Última verificação válida: nenhuma" in out


# --- seed-demo ---------------------------------------------------------------------------------


def test_seed_demo_one_day(demo_env, capsys):
    code, out, _ = run(["seed-demo", "--days", "1"], capsys)
    assert code == EXIT_OK and "24 execuções" in out
    assert db_rows(demo_env, "SELECT COUNT(*), MIN(is_demo), MAX(is_demo) FROM runs") == [(24, 1, 1)]


def test_seed_demo_twice_is_refused(demo_env, capsys):
    run(["seed-demo", "--days", "1"], capsys)
    code, _, err = run(["seed-demo", "--days", "1"], capsys)
    assert code == EXIT_FAIL and "já tem execuções" in err
    assert db_rows(demo_env, "SELECT COUNT(*) FROM runs") == [(24,)]


@pytest.mark.parametrize("days", ["0", "-1"])
def test_seed_demo_non_positive_days_exit_1(demo_env, capsys, days):
    code, _, err = run(["seed-demo", "--days", days], capsys)
    assert code == EXIT_FAIL and "maior que zero" in err


def test_seed_demo_non_numeric_days_is_usage_error(demo_env):
    with pytest.raises(SystemExit) as exc:
        main(["seed-demo", "--days", "abc"])
    assert exc.value.code == 2


def test_seed_demo_refused_when_active_collector_is_real(real_env, capsys):
    code, out, err = run(["seed-demo", "--days", "1"], capsys)
    assert code == EXIT_FAIL
    assert "demo" in err and out == ""
    assert not real_env.db_path.exists()
    assert not (real_env.data_dir / "bankmon-demo.db").exists()


def test_seed_demo_with_busy_lock_exits_2(demo_env, capsys):
    with runner.file_lock(demo_env.lock_path):
        assert run(["seed-demo", "--days", "1"], capsys)[0] == EXIT_LOCKED


def test_seed_then_status_shows_demo_banner_and_populated_sections(project_config, clock, capsys):
    clock.set(find_ok_moment(project_config))
    assert run(["seed-demo", "--days", "1"], capsys)[0] == EXIT_OK
    clock.set(clock.now + 30 * MIN)  # a ultima execucao semeada fica a 30 min
    code, out, _ = run(["status"], capsys)
    assert code == EXIT_OK and "DADOS DE DEMONSTRAÇÃO" in out
    assert out.index("== Com alerta") < out.index("== Sem alerta identificado") < out.index("== Sem cobertura")


# --- backup -----------------------------------------------------------------------------------


def test_backup_without_database_exits_1(demo_env, capsys):
    code, _, err = run(["backup"], capsys)
    assert code == EXIT_FAIL and "banco inexistente" in err
    assert not demo_env.backup_dir.exists()


def test_backup_creates_readable_copy(demo_env, capsys):
    run(["seed-demo", "--days", "1"], capsys)
    code, out, _ = run(["backup"], capsys)
    assert code == EXIT_OK and "Backup gravado em" in out
    files = list(demo_env.backup_dir.glob("bankmon-demo-*.db"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    copy = sqlite3.connect(files[0])
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 24
        assert copy.execute("SELECT COUNT(*) FROM institutions").fetchone()[0] == 2
    finally:
        copy.close()


def test_backup_of_real_database_uses_real_name(real_env, capsys):
    run(["collect"], capsys)
    assert run(["backup"], capsys)[0] == EXIT_OK
    assert len(list(real_env.backup_dir.glob("bankmon-2*.db"))) == 1


# --- uso/argumentos -----------------------------------------------------------------------------


def test_no_command_and_unknown_command_are_usage_errors(demo_env):
    for argv in ([], ["nada"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2


def test_module_entrypoint_runs_check_config_in_subprocess(tmp_path):
    """E1.0: `python -m bankmon` funciona; dados e config isolados via ambiente."""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "BANKMON_DATA_DIR": str(tmp_path / "data"),
        "BANKMON_CONFIG": str(DEFAULT_CONFIG_PATH),
    }
    res = subprocess.run(
        [sys.executable, "-m", "bankmon", "check-config"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "Configuração OK" in res.stdout


def test_module_entrypoint_returns_exit_code_3_for_invalid_config(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("isto não é toml [", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "BANKMON_DATA_DIR": str(tmp_path / "data"),
        "BANKMON_CONFIG": str(bad),
    }
    res = subprocess.run(
        [sys.executable, "-m", "bankmon", "check-config"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert res.returncode == 3 and "Erro de configuração" in res.stderr


def test_two_backups_in_the_same_second_should_not_overwrite(demo_env, clock, capsys):
    run(["init-db"], capsys)
    assert run(["backup"], capsys)[0] == EXIT_OK
    assert run(["backup"], capsys)[0] == EXIT_OK
    assert len(list(demo_env.backup_dir.glob("*.db"))) == 2
