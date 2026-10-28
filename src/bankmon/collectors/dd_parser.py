"""Parser estruturado do HTML da pagina inicial do Downdetector (somente texto local, sem rede).

Cada grafico de servico e um elemento `role="img"` cujo `aria-label` traz o nome e o estado
("Relatos de X nas ultimas 24 horas. Status atual: enfrenta problemas") e cujo SVG declara a cor
no `stroke` de um `path` (ex.: `var(--color-dd-red)`). Texto e cor sao conferidos entre si:
divergencia, cor ausente ou estado desconhecido viram `unknown` com o motivo; nunca `normal`.

Uso: python -m bankmon.collectors.dd_parser <arquivo.html>   (imprime JSON)
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

STATUS_BY_TEXT = {
    "sem problemas": "normal",
    "possiveis problemas": "warning",
    "enfrenta problemas": "critical",
}
STATUS_BY_COLOR = {"blue": "normal", "yellow": "warning", "red": "critical"}
# Desafio/bloqueio REAL: titulo da pagina de desafio, estrutura propria do desafio ou mensagens dele.
# Nao contam sozinhos: a palavra "Cloudflare" (e servico legitimo listado no Downdetector) e o script
# /cdn-cgi/challenge-platform/ (a Cloudflare o injeta tambem em paginas normais - JavaScript Detections).
# Quantificadores limitados e busca so no inicio do documento: sem ReDoS (O(n^2)) com HTML adulterado.
_CF_TITLE = re.compile(
    r"<title\b[^>]{0,200}>\s{0,50}(just a moment|um momento|attention required! \| cloudflare|access denied)",
    re.IGNORECASE)
TITLE_SCAN = 65_536
MESSAGE_SCAN = 262_144  # mensagens de desafio ficam no inicio; evita normalizar o documento inteiro
MAX_LABEL = 512
MAX_BYTES = 5 * 1024 * 1024  # ~30x a pagina real (158 KB); acima disso o CLI recusa
CF_STRUCTURE = ("cf_chl_opt", 'id="challenge-form"', "cf-chl-widget", "cf-error-details",
                "cf-browser-verification", 'id="challenge-error-text"')
CF_MESSAGES = ("verify you are human", "verifique se voce e humano", "confirme que voce e humano",
               "verificando se voce e humano", "checking your browser", "verificando seu navegador",
               "you have been blocked", "voce foi bloqueado", "needs to review the security of your connection",
               "precisa revisar a seguranca da sua conexao")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_LABEL = re.compile(r"^\s*Relatos\s+d[eoa]s?\s+(?P<name>.+?)\s+nas\s+últimas", re.IGNORECASE)
_STATUS = re.compile(r"Status\s+atual\s*:\s*(?P<status>[^.]+)", re.IGNORECASE)
_COLOR = re.compile(r"--color-dd-(red|yellow|blue)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold())
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").split())


class _ChartCollector(HTMLParser):
    """Coleta (aria-label, stroke do primeiro path) de cada grafico de status."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.open: list[dict] = []
        self.charts: list[dict] = []

    def _stroke(self, attrs: dict) -> str | None:
        if attrs.get("stroke"):
            return attrs["stroke"]
        m = re.search(r"stroke\s*:\s*([^;]+)", attrs.get("style") or "")
        return m.group(1).strip() if m else None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self._path(tag, a)
        if tag in _VOID:
            return
        self.depth += 1
        label = a.get("aria-label", "")
        if a.get("role") == "img" and "status atual" in _norm(label):
            self.open.append({"label": label, "depth": self.depth, "stroke": None})

    def handle_startendtag(self, tag, attrs):
        self._path(tag, {k: (v or "") for k, v in attrs})

    def _path(self, tag, a):
        if tag == "path" and self.open and self.open[-1]["stroke"] is None:
            self.open[-1]["stroke"] = self._stroke(a)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        while self.open and self.open[-1]["depth"] >= self.depth:
            self.charts.append(self.open.pop())
        self.depth = max(0, self.depth - 1)

    def close(self):
        super().close()
        while self.open:  # HTML truncado: fecha o que ficou aberto
            self.charts.append(self.open.pop())


def _service(chart: dict) -> dict:
    # Espacos colapsados e tamanho limitado antes das regex (evita ReDoS em `.+?\s+nas`).
    label, stroke = " ".join(chart["label"].split())[:MAX_LABEL], chart["stroke"]
    m_name, m_status = _LABEL.search(label), _STATUS.search(label)
    name = m_name.group("name").strip() if m_name else None
    status_original = m_status.group("status").strip() if m_status else None
    by_text = STATUS_BY_TEXT.get(_norm(status_original)) if status_original else None
    m_color = _COLOR.search(stroke or "")
    color = m_color.group(1).lower() if m_color else ("other" if stroke else None)
    by_color = STATUS_BY_COLOR.get(color)

    issue = None
    if not name:
        issue = "nome_ausente"
    elif by_text is None:
        issue = "estado_textual_desconhecido"
    elif color is None:
        issue = "cor_ausente"
    elif by_color is None:
        issue = "cor_desconhecida"
    elif by_text != by_color:
        issue = "texto_e_cor_divergem"
    consistent = issue is None
    return {
        "service": name,
        "status_original": status_original,
        "status": by_text if consistent else "unknown",
        "color": color,
        "stroke": stroke,
        "source": "dom",
        "consistent": consistent,
        "issue": issue,
    }


def blocked_signals(html: str) -> list[str]:
    """Sinais de pagina de desafio/bloqueio do Cloudflare (lista vazia = nao bloqueada)."""
    low, text = html.casefold(), _norm(html[:MESSAGE_SCAN])
    signals = [f"titulo: {m.group(1)}"] if (m := _CF_TITLE.search(html[:TITLE_SCAN])) else []
    signals += [f"estrutura: {s}" for s in CF_STRUCTURE if s in low]
    signals += [f"mensagem: {s}" for s in CF_MESSAGES if s in text]
    return signals


def parse_html(html: str, min_services: int = 1) -> dict:
    """Interpreta o HTML. `page_state`: ok | blocked_by_cloudflare | incomplete."""
    blocked = blocked_signals(html)
    collector = _ChartCollector()
    collector.feed(html)
    collector.close()
    services = [_service(c) for c in collector.charts]

    counts = Counter(_norm(s["service"]) for s in services if s["service"])
    for s in services:
        s["duplicate"] = bool(s["service"]) and counts[_norm(s["service"])] > 1

    if blocked:
        page_state = "blocked_by_cloudflare"
    elif len(services) < min_services:
        page_state = "incomplete"
    else:
        page_state = "ok"
    summary = Counter(s["status"] for s in services)
    return {
        "page_state": page_state,
        "blocked_markers": blocked,
        "total": len(services),
        "summary": {k: summary.get(k, 0) for k in ("normal", "warning", "critical", "unknown")},
        "inconsistent": sum(not s["consistent"] for s in services),
        "duplicates": sorted({s["service"] for s in services if s["duplicate"]}),
        # Pagina bloqueada/incompleta nao fornece estados validos.
        "services": services if page_state == "ok" else [],
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("uso: python -m bankmon.collectors.dd_parser <arquivo.html>", file=sys.stderr)
        return 2
    with Path(args[0]).open("rb") as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        print(f"arquivo maior que {MAX_BYTES // (1024 * 1024)} MB: recusado", file=sys.stderr)
        return 2
    result = parse_html(data.decode("utf-8", errors="replace"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # no Windows, saida redirecionada seria cp1252
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0 if result["page_state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
