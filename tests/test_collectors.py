"""Coletores: contrato, registry, demo (deterministico) e stub do Downdetector (sem rede)."""

import ast
import importlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import bankmon.collectors as collectors_pkg
from bankmon.collectors import registry
from bankmon.collectors.base import LEVELS, SOURCE_STATUSES, InstitutionRef
from bankmon.collectors.demo import FAILURE_RATE, NOT_LISTED_COUNT, DemoCollector
from bankmon.collectors.downdetector import PENDING_MESSAGE, DowndetectorCollector
from conftest import T0, refs_for

NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "websockets", "websocket", "selenium",
    "playwright", "cloudscraper", "mechanize", "pycurl", "asyncio",
}
COLLECTORS_DIR = Path(collectors_pkg.__file__).parent
HOUR = timedelta(hours=1)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# --- 7. Stub do Downdetector: nenhum codigo de rede ----------------------------------


def test_stub_module_has_no_network_imports():
    roots = _imported_roots(COLLECTORS_DIR / "downdetector.py")
    assert not roots & NETWORK_MODULES, f"imports de rede no stub: {roots & NETWORK_MODULES}"


def test_stub_has_no_dynamic_imports_or_network_calls():
    tree = ast.parse((COLLECTORS_DIR / "downdetector.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"__import__", "eval", "exec", "compile", "open"}
    assert not attrs & {"urlopen", "connect", "create_connection", "getaddrinfo", "import_module"}


@pytest.mark.parametrize("path", sorted(COLLECTORS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_collector_module_imports_network_libraries_in_etapa1(path):
    assert not _imported_roots(path) & NETWORK_MODULES


def test_importing_stub_and_registry_does_not_load_network_modules():
    code = (
        "import sys, bankmon.collectors.downdetector, bankmon.collectors.registry\n"
        "banned = {'socket','http','urllib','requests','httpx','aiohttp'}\n"
        "print(sorted(banned & set(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_stub_returns_not_authorized_without_observations():
    refs = [InstitutionRef("a", "A", "A"), InstitutionRef("b", "B", "B")]
    res = DowndetectorCollector(clock=lambda: T0).collect(refs)
    assert res.source_status == "not_authorized"
    assert res.observations == ()
    assert res.is_demo is False and DowndetectorCollector.is_demo is False
    assert res.collector == "downdetector"
    assert res.error_detail == PENDING_MESSAGE
    assert res.started_at == res.finished_at == T0


def test_stub_ignores_institutions_and_works_with_empty_list():
    assert DowndetectorCollector(clock=lambda: T0).collect([]).source_status == "not_authorized"


def test_stub_from_options_ignores_seed():
    c = DowndetectorCollector.from_options(seed=123, clock=lambda: T0)
    assert isinstance(c, DowndetectorCollector)
    assert c.collect([]).started_at == T0


def test_stub_message_mentions_pending_authorization():
    assert "pendente de autorização" in PENDING_MESSAGE


# --- Registry ----------------------------------------------------------------------------


def test_registry_contains_demo_and_downdetector_keyed_by_name():
    assert set(registry.COLLECTORS) == {"demo", "downdetector", "offline_html", "navegador"}
    for name, cls in registry.COLLECTORS.items():
        assert cls.name == name


def test_registry_unknown_name_raises_clear_error():
    with pytest.raises(KeyError, match="coletor desconhecido"):
        registry.collector_class("nada")
    with pytest.raises(KeyError):
        registry.create_collector("nada", seed=1, clock=lambda: T0)


@pytest.mark.parametrize("name,expected", [("demo", DemoCollector), ("downdetector", DowndetectorCollector)])
def test_create_collector_returns_instances(name, expected):
    assert isinstance(registry.create_collector(name, seed=1, clock=lambda: T0), expected)


def test_registered_collectors_are_importable_from_their_modules():
    for cls in registry.COLLECTORS.values():
        assert getattr(importlib.import_module(cls.__module__), cls.__name__) is cls


# --- Demo -----------------------------------------------------------------------------------


def demo(seed, at):
    return DemoCollector(seed=seed, clock=lambda: at)


def test_demo_is_flagged_and_labelled_synthetic(project_config):
    refs = refs_for(project_config)
    # procura uma hora sem falha simulada
    for h in range(50):
        res = demo(1, T0 + h * HOUR).collect(refs)
        if res.source_status == "ok":
            break
    assert res.is_demo and res.collector == "demo"
    assert all("DEMO" in (o.detail or "") for o in res.observations)


def test_demo_one_observation_per_institution_with_valid_levels(project_config):
    refs = refs_for(project_config)
    assert len(refs) == 15
    for h in range(72):
        res = demo(7, T0 + h * HOUR).collect(refs)
        assert res.source_status in SOURCE_STATUSES
        if res.source_status == "ok":
            assert sorted(o.institution_id for o in res.observations) == sorted(r.id for r in refs)
            assert {o.level for o in res.observations} <= set(LEVELS)
        else:
            assert res.observations == () and res.error_detail


def test_demo_same_seed_and_time_is_identical_and_differs_across_seeds(project_config):
    refs = refs_for(project_config)
    a = [demo(5, T0 + h * HOUR).collect(refs) for h in range(48)]
    b = [demo(5, T0 + h * HOUR).collect(refs) for h in range(48)]
    c = [demo(6, T0 + h * HOUR).collect(refs) for h in range(48)]
    assert a == b
    assert a != c


def test_demo_covers_all_levels_and_simulated_failures_over_a_week(project_config):
    refs = refs_for(project_config)
    levels, statuses = set(), []
    for h in range(7 * 24):
        res = demo(project_config.demo_seed, T0 + h * HOUR).collect(refs)
        statuses.append(res.source_status)
        levels.update(o.level for o in res.observations)
    assert levels == set(LEVELS)
    failures = [s for s in statuses if s != "ok"]
    assert set(failures) <= {"error", "inaccessible"}
    assert 0 < len(failures) <= 0.15 * len(statuses)
    assert FAILURE_RATE < 0.2  # sanidade: a falha simulada e minoria


def test_demo_not_listed_set_is_small_and_stable(project_config):
    refs = refs_for(project_config)
    sets = []
    for h in range(60):
        res = demo(project_config.demo_seed, T0 + h * HOUR).collect(refs)
        if res.source_status == "ok":
            sets.append({o.institution_id for o in res.observations if o.level == "not_listed"})
    assert sets and all(len(s) == NOT_LISTED_COUNT for s in sets)
    assert all(s == sets[0] for s in sets)


def test_demo_with_few_institutions_has_no_not_listed():
    refs = [InstitutionRef(f"i{k}", f"I{k}", f"I{k}") for k in range(7)]
    for h in range(100):
        res = demo(3, T0 + h * HOUR).collect(refs)
        assert all(o.level != "not_listed" for o in res.observations)


def test_demo_handles_empty_institution_list():
    for h in range(30):
        res = demo(3, T0 + h * HOUR).collect([])
        assert res.observations == ()


def test_demo_timestamps_are_utc_aware_and_ordered(project_config):
    res = demo(1, T0).collect(refs_for(project_config))
    assert res.started_at.tzinfo is not None
    assert res.started_at <= res.finished_at


def test_demo_default_clock_is_aware_utc():
    res = DemoCollector(seed=1).collect([InstitutionRef("a", "A", "A")])
    assert res.started_at.utcoffset() == timedelta(0)
