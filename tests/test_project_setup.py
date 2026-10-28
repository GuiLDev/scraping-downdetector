"""E1.0 (preparacao): estrutura do projeto, dependencias, .gitignore, segredos e TOML de exemplo."""

import re
import tomllib
from pathlib import Path

import pytest

from bankmon import db
from bankmon.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config

EXPECTED_AGENTS = {"scraping-specialist", "incident-analyzer", "dashboard-specialist"}
EXPECTED_INSTITUTIONS = {
    "itau", "bradesco", "santander", "banco-do-brasil", "caixa", "btg-pactual",
    "nubank", "inter", "c6-bank", "neon", "next",
    "mercado-pago", "picpay", "pagbank", "pix",
}


@pytest.fixture(scope="module")
def pyproject():
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_expected_layout_exists():
    for rel in (
        "pyproject.toml", ".gitignore", ".env.example", "config/institutions.toml",
        "src/bankmon/config.py", "src/bankmon/db.py", "src/bankmon/schema.sql",
        "src/bankmon/status.py", "src/bankmon/runner.py", "src/bankmon/cli.py",
        "src/bankmon/collectors/base.py", "src/bankmon/collectors/registry.py",
        "src/bankmon/collectors/demo.py", "src/bankmon/collectors/downdetector.py",
    ):
        assert (PROJECT_ROOT / rel).is_file(), rel


def test_pyproject_declares_python_deps_script_and_pytest(pyproject):
    project = pyproject["project"]
    assert project["requires-python"].startswith(">=3.12")
    deps = " ".join(project["dependencies"]).lower()
    for needed in ("flask", "waitress", "tzdata"):
        assert needed in deps
    assert any(d.lower().startswith("pytest") for d in project["optional-dependencies"]["dev"])
    assert project["scripts"]["bankmon"] == "bankmon.cli:main"
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_pyproject_has_no_network_scraping_dependencies(pyproject):
    """Etapa 1 nao deve trazer libs de coleta/evasao."""
    deps = " ".join(pyproject["project"]["dependencies"]).lower()
    for banned in ("requests", "httpx", "aiohttp", "selenium", "playwright", "cloudscraper", "undetected"):
        assert banned not in deps


def test_schema_file_is_packaged(pyproject):
    assert "schema.sql" in pyproject["tool"]["setuptools"]["package-data"]["bankmon"]
    assert db.SCHEMA_PATH.is_file()


def test_gitignore_protects_secrets_data_and_artifacts():
    lines = {
        l.strip() for l in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    for entry in (".env", "data/", ".venv/", "__pycache__/", "*.db", "*.log"):
        assert entry in lines, f"{entry} deveria estar no .gitignore"


def test_env_example_has_no_secret_values():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assignments = [
        l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#") and "=" in l
    ]
    for line in assignments:
        key, _, value = line.partition("=")
        assert value.strip() == "", f"valor em .env.example: {key}"
    assert not re.search(r"(?i)(token|secret|password|senha|api[_-]?key)\s*[:=]\s*\S{6,}", text)


def test_no_real_env_file_in_repo_dir():
    assert not (PROJECT_ROOT / ".env").exists()


def test_source_has_no_hardcoded_credentials():
    pattern = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*=\s*['\"][^'\"]{4,}['\"]")
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_project_agents_exist_with_matching_front_matter():
    agents_dir = PROJECT_ROOT / ".claude" / "agents"
    found = {p.stem for p in agents_dir.glob("*.md")}
    assert EXPECTED_AGENTS <= found
    for name in EXPECTED_AGENTS:
        text = (agents_dir / f"{name}.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front = text.split("---", 2)[1]
        assert re.search(rf"^name:\s*{re.escape(name)}\s*$", front, re.M)
        assert re.search(r"^description:\s*\S", front, re.M)


def test_scraping_specialist_forbids_circumvention():
    text = (PROJECT_ROOT / ".claude" / "agents" / "scraping-specialist.md").read_text(encoding="utf-8")
    assert "Cloudflare" in text and "Contornar" in text


def test_example_toml_matches_the_approved_plan():
    raw = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw["collector"]["active"] == "demo"
    assert raw["schedule"] == {"interval_minutes": 60, "stale_after_minutes": 90}
    assert raw["server"] == {"host": "127.0.0.1", "port": 8765}
    assert {i["id"] for i in raw["institutions"]} == EXPECTED_INSTITUTIONS
    assert all(i["dd_label"].strip() for i in raw["institutions"])
    assert len(raw["categories"]) == 4


def test_example_toml_loads_and_syncs_into_a_fresh_database(project_config):
    assert project_config.is_demo and project_config.db_path.name == "bankmon-demo.db"
    conn = db.open_db(project_config)
    try:
        assert conn.execute("SELECT COUNT(*) FROM institutions WHERE enabled = 1").fetchone()[0] == 15
        assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 4
    finally:
        conn.close()


def test_tests_never_use_the_real_data_dir(project_config):
    assert project_config.data_dir != PROJECT_ROOT / "data"
    assert PROJECT_ROOT not in project_config.data_dir.parents


def test_default_data_dir_is_inside_project_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("BANKMON_DATA_DIR", raising=False)
    monkeypatch.delenv("BANKMON_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.data_dir == Path(PROJECT_ROOT) / "data"  # so le o caminho; nada e criado aqui
