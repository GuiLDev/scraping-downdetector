"""Validacao estrita da geometria SVG vinda de HTML externo (funcoes puras, sem I/O).

A geometria e guardada no banco e, depois de REVALIDADA na leitura, renderizada pelo dashboard
como atributo SVG autoescapado (nunca como HTML cru). Cada funcao devolve o valor normalizado ou
None se invalido; quem chama registra o motivo em `issue`.
"""

from __future__ import annotations

import math
import re

MAX_DIMENSION = 10_000.0
MAX_PATH_LEN = 20_000
MAX_VIEWBOX_LEN = 200
MAX_COORD = 1e6

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
# re.ASCII: \d e \s so casam ASCII (sem digitos arabes/fullwidth nem espacos Unicode).
_NUM_RE = re.compile(_NUM, re.ASCII)
_VIEWBOX_RE = re.compile(rf"\s*{_NUM}(?:[\s,]+{_NUM}){{3}}\s*", re.ASCII)
_DIMENSION_RE = re.compile(rf"\s*({_NUM})\s*(?:px)?\s*", re.ASCII | re.IGNORECASE)
# Separadores (espaco/virgula) e depois um comando ou um numero.
_PATH_TOKEN = re.compile(rf"[\s,]*(?:([MmLlHhVvCcSsQqTtAaZz])|({_NUM}))", re.ASCII)


def _finite(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) and abs(value) <= MAX_COORD else None


def validate_viewbox(value: str | None) -> str | None:
    """4 numeros finitos, largura e altura em (0, 10000]. Devolve "x y w h" normalizado."""
    if (
        not isinstance(value, str)
        or not value.isascii()
        or len(value) > MAX_VIEWBOX_LEN
        or not _VIEWBOX_RE.fullmatch(value)
    ):
        return None
    tokens = _NUM_RE.findall(value)
    numbers = [_finite(t) for t in tokens]
    if len(numbers) != 4 or any(n is None for n in numbers):
        return None
    if not (0 < numbers[2] <= MAX_DIMENSION and 0 < numbers[3] <= MAX_DIMENSION):
        return None
    return " ".join(tokens)


def validate_dimension(value: str | None) -> str | None:
    """Numero finito em [0, 10000], com sufixo opcional "px". Devolve so o numero."""
    if not isinstance(value, str) or not value.isascii() or len(value) > MAX_VIEWBOX_LEN:
        return None
    m = _DIMENSION_RE.fullmatch(value)
    if not m:
        return None
    number = _finite(m.group(1))
    if number is None or not 0 <= number <= MAX_DIMENSION:
        return None
    return m.group(1)


def validate_path_d(value: str | None) -> str | None:
    """Gramatica estrita: so comandos SVG, numeros finitos e separadores; comeca com M/m."""
    if not isinstance(value, str) or not value.isascii() or len(value) > MAX_PATH_LEN:
        return None
    text = value.strip()
    if not text or text[0] not in "Mm":
        return None
    pos = 0
    while pos < len(text):
        m = _PATH_TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            return None
        if m.group(2) is not None and _finite(m.group(2)) is None:
            return None
        pos = m.end()
    return " ".join(text.split())
