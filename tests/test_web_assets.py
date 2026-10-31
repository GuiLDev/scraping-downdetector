"""Assets vendored e arquivos estaticos/templates proprios: integridade e ausencia de URLs externas."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "bankmon" / "web"
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
VENDORED = STATIC / "VENDORED.md"


def vendored_hashes() -> dict[str, str]:
    rows = re.findall(r"^\| `([^`]+)` \| `[^`]+` \| `([0-9a-f]{64})` \|$", VENDORED.read_text("utf-8"), re.M)
    return dict(rows)


def test_vendored_md_lists_all_four_assets():
    assert set(vendored_hashes()) == {
        "vendor/pico/pico.min.css",
        "vendor/pico/LICENSE.md",
        "vendor/chartjs/chart.umd.min.js",
        "vendor/chartjs/LICENSE.md",
    }


@pytest.mark.parametrize("rel,expected", sorted(vendored_hashes().items()))
def test_asset_sha256_matches_vendored_md(rel, expected):
    assert hashlib.sha256((STATIC / rel).read_bytes()).hexdigest() == expected


def test_no_unregistered_files_under_vendor():
    listed = set(vendored_hashes())
    actual = {p.relative_to(STATIC).as_posix() for p in (STATIC / "vendor").rglob("*") if p.is_file()}
    assert actual == listed


def test_vendored_md_documents_versions_and_origin():
    text = VENDORED.read_text("utf-8")
    assert "@picocss/pico" in text and "chart.js" in text
    assert "https://registry.npmjs.org/@picocss/pico/-/pico-" in text
    assert "https://registry.npmjs.org/chart.js/-/chart.js-" in text
    assert re.search(r"pico-2\.\d+\.\d+\.tgz", text) and re.search(r"chart\.js-4\.\d+\.\d+\.tgz", text)


def test_licenses_present():
    for rel in ("vendor/pico/LICENSE.md", "vendor/chartjs/LICENSE.md"):
        assert "MIT" in (STATIC / rel).read_text("utf-8")


def test_templates_have_no_external_references():
    files = list(TEMPLATES.glob("*.html"))
    assert files
    for f in files:
        text = f.read_text("utf-8")
        assert not re.search(r"""(src|href|action)\s*=\s*["']?(https?:)?//""", text), f.name
        assert "|safe" not in text and "Markup(" not in text, f.name
        assert "{% autoescape false" not in text, f.name


def test_own_static_files_have_no_external_urls():
    for name in ("app.js", "app.css"):
        text = (STATIC / name).read_text("utf-8")
        assert not re.search(r"https?://", text), name
        assert "@import" not in text, name


def test_app_js_has_no_dynamic_code_or_html_injection():
    text = (STATIC / "app.js").read_text("utf-8")
    for bad in ("eval(", "new Function", "innerHTML", "document.write", "XMLHttpRequest"):
        assert bad not in text, bad
    # unico fetch permitido: a versao dos dados do proprio painel (mesma origem, so leitura)
    assert text.count("fetch(") == 1 and 'fetch("/painel/versao"' in text


def test_static_files_are_served_with_correct_types(tmp_path, monkeypatch):
    from webutil import client_for, make_cfg

    client = client_for(make_cfg(tmp_path, monkeypatch))
    for rel in ("vendor/pico/pico.min.css", "vendor/chartjs/chart.umd.min.js", "app.js", "app.css"):
        resp = client.get("/static/" + rel)
        assert resp.status_code == 200, rel
    assert "javascript" in client.get("/static/app.js").content_type
    assert "css" in client.get("/static/app.css").content_type


def test_package_data_patterns_cover_web_files():
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text("utf-8"))["tool"]["setuptools"]["package-data"]["bankmon"]
    assert "web/templates/*.html" in data and "web/static/**/*" in data
