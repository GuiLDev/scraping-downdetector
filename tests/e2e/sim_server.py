"""Pagina de SIMULACAO local para os testes (nunca acessa nem copia um site real).

Serve, so em 127.0.0.1, um HTML no mesmo formato lido pelo parser (graficos `role="img"` com
`aria-label` "Relatos de X ... Status atual: ...", `svg` com `viewBox` e `path` com `stroke`),
usando os nomes das instituicoes configuradas. O modo muda por
`GET /_sim/modo?valor=normal_a|normal_b|bloqueio`; `bloqueio` simula uma pagina de desafio, com o
botao "Sou humano (simulacao)": o CLIQUE do usuario faz `POST /_sim/resolver` (volta ao conteudo
normal) e a propria pagina se recarrega. Nada disso depende da extensao. `GET /_sim/contagem`
informa quantas vezes a pagina (`/` ou `/dd`) foi pedida (prova de recarga).

Uso manual: python tests/e2e/sim_server.py [--port 8777]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MODES = ("normal_a", "normal_b", "bloqueio")
DEFAULT_PORT = 8777
HOST = "127.0.0.1"
FILLERS = 8
COLORS = {"sem problemas": "blue", "possíveis problemas": "yellow", "enfrenta problemas": "red"}


def _status_of(index: int, mode: str) -> str:
    """normal_a: o 1o servico com possiveis problemas; normal_b: o 2o com problemas."""
    if mode == "normal_a" and index == 0:
        return "possíveis problemas"
    if mode == "normal_b" and index == 1:
        return "enfrenta problemas"
    return "sem problemas"


def _path_d(name: str, mode: str) -> str:
    """Curva deterministica por (servico, modo): o grafico muda quando o modo muda."""
    digest = hashlib.sha256(f"{name}|{mode}".encode()).digest()
    points = [f"{i * 24} {38 - digest[i] % 36}" for i in range(11)]
    return "M" + " L".join(points)


def _chart(name: str, status: str, mode: str) -> str:
    label = f"Relatos de {name} nas últimas 24 horas. Status atual: {status}"
    color = COLORS[status]
    return (
        f'<div role="img" aria-label="{label}">'
        f'<svg viewBox="0 0 243 40" width="243" height="40">'
        f'<path d="{_path_d(name, mode)}" stroke="var(--color-dd-{color})" fill="none"></path>'
        "</svg></div>"
    )


def render(mode: str, labels: list[str]) -> str:
    """HTML da pagina simulada no `mode` dado."""
    if mode not in MODES:
        raise ValueError(f"modo desconhecido: {mode!r}")
    if mode == "bloqueio":
        return (
            "<!doctype html><html><head><title>Just a moment...</title></head><body>"
            "<h1>Verificando se você é humano...</h1>"
            '<button id="humano" type="button">Sou humano (simulação)</button>'
            "<script>window._cf_chl_opt = {cvId: '3', cType: 'managed'};"
            "document.getElementById('humano').addEventListener('click', function () {"
            "fetch('/_sim/resolver', {method: 'POST'}).then(function () { location.reload(); });"
            "});</script></body></html>"
        )
    names = [f"Servico simulado {i + 1}" for i in range(FILLERS)] + list(labels)
    charts = "".join(_chart(n, "sem problemas", mode) for n in names[:FILLERS])
    charts += "".join(_chart(n, _status_of(i, mode), mode) for i, n in enumerate(labels))
    return f"<!doctype html><html><head><title>Status simulado</title></head><body>{charts}</body></html>"


class SimState:
    def __init__(self, labels: list[str], mode: str = "normal_a") -> None:
        self.labels = list(labels)
        self.mode = mode
        self.page_hits = 0  # pedidos da pagina (recargas); chamadas de controle nao contam


def respond(path: str, query: str, state: SimState, method: str = "GET") -> tuple[int, str, bytes]:
    """Resposta pura (status, content-type, corpo); testavel sem rede."""
    text = "text/plain; charset=utf-8"
    if path == "/_sim/resolver":
        if method != "POST":
            return 405, text, b"use POST"
        if state.mode == "bloqueio":  # so o desafio e resolvido pelo clique humano
            state.mode = "normal_a"
        return 200, text, state.mode.encode("ascii")
    if method != "GET":
        return 405, text, b"metodo nao permitido"
    if path in ("/", "/dd"):
        state.page_hits += 1
        return 200, "text/html; charset=utf-8", render(state.mode, state.labels).encode("utf-8")
    if path == "/_sim/modo":
        value = parse_qs(query).get("valor", [""])[0]
        if value not in MODES:
            return 400, text, b"modo invalido"
        state.mode = value
        return 200, text, value.encode("ascii")
    if path == "/_sim/contagem":
        return 200, "application/json", json.dumps({"paginas": state.page_hits}).encode("ascii")
    return 404, text, b"nao encontrado"


def make_server(state: SimState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            self._answer("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._answer("POST")

        def _answer(self, method: str) -> None:
            parts = urlsplit(self.path)
            status, ctype, body = respond(parts.path, parts.query, state, method)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silencioso nos testes
            pass

    return ThreadingHTTPServer((HOST, port), Handler)


def main() -> None:
    from bankmon.config import load_config

    parser = argparse.ArgumentParser(description="Pagina de simulacao local (so 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    cfg = load_config()
    labels = [i.dd_label for i in cfg.institutions if i.enabled]
    server = make_server(SimState(labels), args.port)
    print(f"Simulação em http://{HOST}:{args.port}/dd  (modos: {', '.join(MODES)})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
