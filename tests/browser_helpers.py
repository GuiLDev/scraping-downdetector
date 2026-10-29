"""Helpers dos testes da coleta pelo navegador: HTML sintetico no formato real (nunca o real)."""

from __future__ import annotations

BLUE, YELLOW, RED = (f"var(--color-dd-{c})" for c in ("blue", "yellow", "red"))
SVG = 'viewBox="0 0 243 40" width="243" height="40"'
PATH_D = "M0 30 L10 20 L20 25"
URL = "http://127.0.0.1:8777/dd"
ESTADO = "/api/coleta/estado?alvo=http%3A%2F%2F127.0.0.1%3A8777%2Fdd%2F"


def chart(name: str, status: str = "sem problemas", color: str = BLUE, d: str = PATH_D) -> str:
    label = f"Relatos de {name} nas últimas 24 horas. Status atual: {status}"
    return (f'<div role="img" aria-label="{label}"><svg {SVG}>'
            f'<path d="{d}" stroke="{color}"></path></svg></div>')


def page_html(alfa: str = "sem problemas", beta: str = "sem problemas", fillers: int = 8) -> str:
    """Pagina "ok": 4 instituicoes do webutil (Alfa/Beta/Gama/Delta) + preenchimento."""
    colors = {"sem problemas": BLUE, "possíveis problemas": YELLOW, "enfrenta problemas": RED}
    body = [chart(f"Servico {i}") for i in range(fillers)]
    body += [
        chart("Banco Alfa", alfa, colors[alfa]),
        chart("Banco Beta", beta, colors[beta]),
        chart("Banco Gama"),
        chart("Banco Delta"),
    ]
    return f"<html><head><title>Status</title></head><body>{''.join(body)}</body></html>"


def blocked_html() -> str:
    return (
        "<html><head><title>Just a moment...</title></head><body>"
        "<script>window._cf_chl_opt = {cvId: '3'};</script></body></html>"
    )
