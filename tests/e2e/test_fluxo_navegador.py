"""E2E: coleta pela aba do navegador (extensao MV3 no Edge) + intervencao humana.

So paginas locais (simulador em 127.0.0.1). Sem Downdetector real, sem CAPTCHA, sem cookies.
Padrao da skill webapp-testing (with_server.py): subir servidores, esperar a porta, rodar, encerrar;
aqui feito por fixtures de pytest (subprocessos + espera de porta + terminate) para um unico
navegador/ciclo por modulo. Dados em diretorios temporarios; portas livres (nunca 8765).

Rode: pytest -m e2e tests/e2e -q
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from bankmon.config import load_config
from sim_server import SimState, make_server

playwright_sync = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PROJECT_DATA = ROOT / "data"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"porta {port} nao abriu")


def data_hashes() -> dict[str, str]:
    out = {}
    if PROJECT_DATA.is_dir():
        for p in sorted(PROJECT_DATA.glob("*.db*")):
            try:
                out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[p.name] = "ilegivel"
    return out


def http_json(url: str, token: str):
    req = urllib.request.Request(url, headers={"X-BankMonitor-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def sim_pages(env) -> int:
    """Quantas vezes o simulador serviu a pagina (cada recarga conta; consultas de controle nao)."""
    with urllib.request.urlopen(f"{env['sim']}/_sim/contagem", timeout=5) as r:
        return json.loads(r.read())["paginas"]


def http_post(url: str, token: str, body: dict) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"X-BankMonitor-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    hashes_before = data_hashes()
    tmp = tmp_path_factory.mktemp("e2e")
    data_dir = tmp / "data"
    data_dir.mkdir()
    sim_port, dash_port = free_port(), free_port()
    assert 8765 not in (sim_port, dash_port)

    toml = (ROOT / "config" / "institutions.toml").read_text(encoding="utf-8")
    toml, n = re.subn(r"(?m)^port\s*=\s*\d+", f"port = {dash_port}", toml, count=1)
    assert n == 1
    assert "[coleta_navegador]" not in toml  # a config do projeto nao lista origens (SEC-015)
    # lista EXPLICITA com a origem exata do simulador; intervalo 0 para o teste nao esperar
    toml += (f'\n[coleta_navegador]\norigens_autorizadas = ["http://127.0.0.1:{sim_port}"]\n'
             "intervalo_minimo_local_s = 0\n")
    cfg_path = tmp / "institutions.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    environ = {**os.environ, "BANKMON_DATA_DIR": str(data_dir), "BANKMON_CONFIG": str(cfg_path),
               "PYTHONIOENCODING": "utf-8"}

    old = {k: os.environ.get(k) for k in ("BANKMON_DATA_DIR", "BANKMON_CONFIG")}
    os.environ["BANKMON_DATA_DIR"], os.environ["BANKMON_CONFIG"] = str(data_dir), str(cfg_path)
    try:
        cfg = load_config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    labels = [i.dd_label for i in cfg.institutions if i.enabled]
    names = [i.name for i in cfg.institutions if i.enabled]

    state = SimState(labels, "normal_a")
    sim = make_server(state, sim_port)
    threading.Thread(target=sim.serve_forever, daemon=True).start()
    wait_port(sim_port)

    py = sys.executable
    target = f"http://127.0.0.1:{sim_port}/dd"
    gen = subprocess.run([py, "-m", "bankmon", "extensao", "--alvo", target, "--intervalo", "60"],
                         env=environ, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert gen.returncode == 0, gen.stderr
    ext_dir = data_dir / "extensao-coletor"
    assert (ext_dir / "config.json").is_file()
    token = json.loads((ext_dir / "config.json").read_text(encoding="utf-8"))["token"]

    dash = subprocess.Popen([py, "-m", "bankmon", "serve", "--navegador"], env=environ, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(dash_port)
        yield {
            "sim_port": sim_port, "dash_port": dash_port, "target": target, "ext_dir": ext_dir,
            "token": token, "names": names, "state": state, "tmp": tmp, "data_dir": data_dir,
            "hashes_before": hashes_before,
            "api": f"http://127.0.0.1:{dash_port}",
            "sim": f"http://127.0.0.1:{sim_port}",
        }
    finally:
        dash.terminate()
        try:
            dash.wait(timeout=10)
        except subprocess.TimeoutExpired:
            dash.kill()
        sim.shutdown()
        sim.server_close()


@pytest.fixture(scope="module")
def browser(env):
    from playwright.sync_api import sync_playwright

    requests: list[str] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(env["tmp"] / "profile"), channel="msedge", headless=True,
            args=[f"--disable-extensions-except={env['ext_dir']}", f"--load-extension={env['ext_dir']}"],
        )
        try:
            ctx.on("request", lambda r: requests.append(r.url))
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=30000)
            sim_page = ctx.new_page()
            sim_page.goto(env["target"])
            dash_page = ctx.new_page()
            dash_page.goto(env["api"] + "/")
            dash_page.bring_to_front()
            yield {"ctx": ctx, "sw": sw, "sim_page": sim_page, "dash": dash_page, "requests": requests}
        finally:
            ctx.close()


def set_mode(env, mode: str) -> None:
    with urllib.request.urlopen(f"{env['sim']}/_sim/modo?valor={mode}", timeout=5) as r:
        assert r.status == 200


def collect(browser) -> dict:
    return browser["sw"].evaluate("() => coletarAgora()")


def active_tab_url(browser) -> str:
    return browser["sw"].evaluate(
        "async () => { const t = await chrome.tabs.query({active: true, currentWindow: true});"
        " return t[0] ? t[0].url : null; }"
    )


def run_state(env) -> dict:
    alvo = urllib.parse.quote(env["target"] + "/", safe="")
    return http_json(env["api"] + f"/api/coleta/estado?alvo={alvo}", env["token"])


def last_run_status(env) -> str | None:
    import sqlite3
    db = env["data_dir"] / "bankmon-navegador.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT source_status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def count_runs(env) -> int:
    import sqlite3
    db = env["data_dir"] / "bankmon-navegador.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def card(page, name: str):
    return page.locator("article.card").filter(has=page.locator(".card-name", has_text=name)).first


def reload_dash(browser):
    page = browser["dash"]
    page.reload()
    page.wait_for_load_state("networkidle")
    return page


def test_cenarios_em_ordem(env, browser):
    """Os cenarios dependem uns dos outros (estado do servidor); rodam em sequencia unica."""
    names = env["names"]
    dash = browser["dash"]
    dash_url = env["api"] + "/"

    # ---- Cenario A: normal ----
    hits0 = sim_pages(env)
    res = collect(browser)
    assert res == {"resultado": "enviado", "http": 200}, res
    assert sim_pages(env) > hits0, "a aba do simulador nao foi recarregada"
    assert active_tab_url(browser).startswith(dash_url), "a coleta ativou a aba do simulador"
    assert last_run_status(env) == "ok"
    assert run_state(env)["estado"] == "ativo"
    page = reload_dash(browser)
    assert card(page, names[0]).locator(".card-state").inner_text().strip() != ""
    texto_a0 = card(page, names[0]).inner_text()
    texto_a1 = card(page, names[1]).inner_text()
    assert "Atenção" in texto_a0, texto_a0
    assert "Sem alerta" in texto_a1, texto_a1
    assert page.locator("svg.dd-chart").count() > 0
    assert page.locator("form.resume-form").count() == 0
    chart_a = page.locator("article.card svg.dd-chart path").first.get_attribute("d")

    set_mode(env, "normal_b")
    assert collect(browser) == {"resultado": "enviado", "http": 200}
    assert active_tab_url(browser).startswith(dash_url)
    page = reload_dash(browser)
    texto_b0 = card(page, names[0]).inner_text()
    texto_b1 = card(page, names[1]).inner_text()
    assert "Atenção" not in texto_b0 and "Crítico" not in texto_b0
    assert "Crítico" in texto_b1, texto_b1
    assert "Sem alerta" in texto_b0, texto_b0
    assert page.locator("article.card svg.dd-chart path").first.get_attribute("d") != chart_a
    assert run_state(env)["estado"] == "ativo"

    # ---- Cenario B: bloqueio ----
    set_mode(env, "bloqueio")
    runs_before = count_runs(env)
    res = collect(browser)
    assert res["resultado"] == "enviado", res
    assert last_run_status(env) == "inaccessible"
    assert run_state(env)["estado"] == "pausado"
    page = reload_dash(browser)
    assert "Coleta pausada" in page.locator(".notices, .notice").first.inner_text() or \
        page.get_by_text("Coleta pausada").count() > 0
    assert page.get_by_role("button", name="Retomar verificação").is_visible()
    c0 = card(page, names[0])
    assert "Sem dados" in c0.inner_text()
    assert "Última leitura válida (desatualizada)" in c0.inner_text()
    assert page.locator("article.card svg.dd-chart").count() == 0
    assert page.locator("article.card.state-no-alert").count() == 0, "verde com dados desatualizados"
    assert page.locator("li.count.state-no-alert .count-n").inner_text().strip() == "0"
    runs_paused = count_runs(env)
    assert runs_paused == runs_before + 1

    res = collect(browser)
    assert res == {"resultado": "pausado"}, res
    assert count_runs(env) == runs_paused, "coleta pausada gravou algo"
    # a API recusa mesmo se alguem tentar enviar direto
    assert http_post(env["api"] + "/api/coleta", env["token"], {"url": env["target"], "html": "<html></html>"}) == 409
    assert count_runs(env) == runs_paused
    # a reserva tambem e recusada (409 pausado): nenhuma recarga seria autorizada
    assert http_post(env["api"] + "/api/coleta/reserva", env["token"], {"alvo": env["target"]}) == 409

    # ---- Variante: retoma ainda bloqueado -> le o DOM atual (sem recarga) e volta a pausar ----
    hits = sim_pages(env)
    page.get_by_role("button", name="Retomar verificação").click()
    page.wait_for_load_state("networkidle")
    assert run_state(env)["estado"] == "verificando"
    assert page.get_by_text("Verificação solicitada").count() > 0
    assert sim_pages(env) == hits, "retomar recarregou a aba"
    res = collect(browser)
    assert res["resultado"] == "enviado", res
    assert sim_pages(env) == hits, "a verificacao recarregou a aba"
    assert run_state(env)["estado"] == "pausado"
    page = reload_dash(browser)
    assert page.get_by_role("button", name="Retomar verificação").is_visible()

    # ---- Cenario C: o HUMANO resolve o desafio na aba do simulador e retoma na dashboard ----
    sim_page = browser["sim_page"]
    assert "Just a moment" in sim_page.title()
    sim_page.get_by_role("button", name="Sou humano (simulação)").click()
    sim_page.wait_for_selector('[role="img"]')  # a propria pagina se recarregou com o conteudo normal
    hits = sim_pages(env)
    page.get_by_role("button", name="Retomar verificação").click()
    page.wait_for_load_state("networkidle")
    assert run_state(env)["estado"] == "verificando"
    assert page.get_by_text("Verificação solicitada").count() > 0
    res = collect(browser)
    assert res == {"resultado": "enviado", "http": 200}, res
    assert sim_pages(env) == hits, "a verificacao recarregou a aba (deveria ler o DOM existente)"
    assert last_run_status(env) == "ok"
    assert run_state(env)["estado"] == "ativo"
    page = reload_dash(browser)
    assert page.get_by_text("Coleta pausada").count() == 0
    assert page.locator("form.resume-form").count() == 0
    assert "Sem dados" not in card(page, names[0]).inner_text()
    assert "Atenção" in card(page, names[0]).inner_text()  # "desde HH:MM" varia; compara so o rotulo
    assert page.locator("svg.dd-chart").count() > 0
    assert active_tab_url(browser).startswith(dash_url)
    # depois da retomada o fluxo normal volta: reserva -> recarga (intervalo 0 no teste)
    assert collect(browser) == {"resultado": "enviado", "http": 200}
    assert sim_pages(env) > hits


def test_garantias(env, browser):
    # so loopback (registro de todas as requisicoes do contexto)
    from urllib.parse import urlsplit
    hosts = {urlsplit(u).hostname for u in browser["requests"] if u.startswith(("http:", "https:"))}
    # Excecao de AMBIENTE (nao do projeto): o antivirus Kaspersky da maquina injeta um script nas
    # paginas e este pede um recurso proprio. Qualquer outro host externo reprova.
    hosts = {h for h in hosts if not (h or "").endswith(".kaspersky-labs.com")}
    assert hosts == {"127.0.0.1"}, hosts
    assert browser["requests"], "nenhuma requisicao registrada"

    # extensao: sem cookies
    js = (env["ext_dir"] / "background.js").read_text(encoding="utf-8")
    code = "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))
    assert "document.cookie" not in code and "chrome.cookies" not in code
    assert "cookies" not in json.loads((env["ext_dir"] / "manifest.json").read_text())["permissions"]
    assert not re.search(r"cookie", code, re.I)


def test_data_do_projeto_intacto(env, browser):
    # roda por ultimo (ordem do arquivo): bancos do projeto iguais ao inicio do modulo
    assert data_hashes() == env["hashes_before"]
