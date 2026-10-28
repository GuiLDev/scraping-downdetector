"""Dashboard web (Flask): somente leitura, somente GET, somente loopback.

O dashboard nunca grava no banco: abre uma conexao `mode=ro` por requisicao e
calcula todos os estados via `bankmon.status`.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask, render_template, request, send_from_directory
from flask.globals import request_ctx
from werkzeug.exceptions import HTTPException

from bankmon.config import Config
from bankmon.timeutil import utc_now
from bankmon.web import views

# "[::1]" nao entra aqui: o Werkzeug nunca o casa (bug no parse de IPv6); o caso e coberto
# pelo wrapper `_normalize_host`, que o reescreve para "localhost" antes da checagem.
TRUSTED_HOSTS = ["127.0.0.1", "localhost"]
STATIC_DIR = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}

ERROR_TEXTS = {
    400: ("Requisição inválida", "O endereço usado para acessar o painel não é permitido."),
    404: ("Página não encontrada", "O endereço não existe ou a instituição não está configurada."),
    405: ("Método não permitido", "Este painel é somente leitura."),
}


_IPV6_LOOPBACK_HOST = re.compile(r"\[::1\](:\d{1,5})?")


def _normalize_host(wsgi_app):
    """Normaliza o Host antes da checagem de TRUSTED_HOSTS: (1) minusculas, pois o Host e
    case-insensitive ("LOCALHOST" == "localhost"); isso nao amplia a lista confiavel.
    (2) Werkzeug 3.1 nao consegue confiar em hosts IPv6 (`ref.partition(":")` quebra "::1"
    e "[::1]"), entao o Host `[::1]:porta` seria sempre rejeitado. Este wrapper reescreve
    somente o literal exato `[::1]` (com porta opcional) para `localhost`, que ja esta na
    lista confiavel. Qualquer outro Host continua sujeito ao TRUSTED_HOSTS normal."""

    def middleware(environ, start_response):
        host = environ.get("HTTP_HOST")
        if host:
            environ["HTTP_HOST"] = host = host.lower()
        m = _IPV6_LOOPBACK_HOST.fullmatch(host or "")
        if m:
            environ["HTTP_HOST"] = "localhost" + (m.group(1) or "")
        return wsgi_app(environ, start_response)

    return middleware


def create_app(cfg: Config, clock: Callable[[], datetime] = utc_now) -> Flask:
    app = Flask(__name__, static_folder=None)  # /static registrada abaixo (so GET/HEAD)
    app.config["TRUSTED_HOSTS"] = list(TRUSTED_HOSTS)
    app.config["DEBUG"] = False
    app.jinja_env.autoescape = True
    app.jinja_env.filters["local_hm"] = views.hm
    app.jinja_env.globals.update(
        DISCLAIMER=views.DISCLAIMER,
        DEMO_BANNER=views.DEMO_BANNER,
        STATE_UI=views.STATE_UI,
    )

    def get_route(rule: str):
        # Somente GET (HEAD vem junto); sem OPTIONS automatico -> demais metodos dao 405.
        return app.route(rule, methods=["GET"], provide_automatic_options=False)

    @app.route("/static/<path:filename>", endpoint="static", methods=["GET"], provide_automatic_options=False)
    def static(filename: str):
        # send_from_directory usa safe_join: path traversal -> 404.
        return send_from_directory(STATIC_DIR, filename)

    @get_route("/")
    def index():
        return render_template("index.html", **views.build_index(cfg, request.args, clock()))

    @get_route("/history")
    def history():
        return render_template("history.html", **views.build_history(cfg, request.args, clock()))

    @get_route("/institution/<institution_id>")
    def institution(institution_id: str):
        ctx = views.build_institution(cfg, institution_id, request.args, clock())
        if ctx is None:
            return error_page(404)
        return render_template("institution.html", **ctx)

    def error_page(code: int):
        title, text = ERROR_TEXTS.get(code, ("Erro", "Não foi possível concluir a requisição."))
        if request_ctx.url_adapter is None:
            # Host nao confiavel: sem adaptador de URL nao da para renderizar o layout
            # (url_for falharia); devolve uma pagina minima estatica.
            body = f"<!doctype html><meta charset=utf-8><title>{title}</title><h1>{title}</h1><p>{text}</p>"
            return body, code
        html = render_template("error.html", code=code, title=title, text=text, demo=cfg.is_demo)
        return html, code

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        resp = error_page(exc.code or 500)
        if exc.code == 405 and exc.valid_methods:
            return resp[0], 405, {"Allow": ", ".join(exc.valid_methods)}
        return resp

    @app.errorhandler(500)
    def server_error(_exc):
        return error_page(500)

    @app.after_request
    def set_security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.wsgi_app = _normalize_host(app.wsgi_app)  # type: ignore[method-assign]
    return app
