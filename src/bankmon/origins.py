"""Origens autorizadas para a coleta pelo navegador.

Nao ha origem implicita: so valem as origens EXATAS listadas em `[coleta_navegador]
origens_autorizadas` (ver `config.py`); externa exige tambem `autorizacao_ref`. Uma origem e
`esquema://host[:porta]` (sem porta = porta padrao do esquema). Nenhum curinga, nem no host nem na porta.
"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_LEN = 480  # igual ao limite da URL gravada (runner._SOURCE_URL)
_PATTERN = re.compile(r"(https?)://([a-z0-9]([a-z0-9.-]*[a-z0-9])?)(?::(\d{1,5}))?")


def parse_pattern(pattern: str) -> tuple[str, str, int]:
    """`(esquema, host, porta)` de uma origem exata. Levanta ValueError se for invalida."""
    m = _PATTERN.fullmatch(pattern.strip().lower()) if isinstance(pattern, str) else None
    if m is None:
        raise ValueError(f"origem inválida: {pattern!r} (use esquema://host[:porta], sem curinga)")
    scheme, host, _, port = m.groups()
    number = int(port) if port else DEFAULT_PORTS[scheme]
    if not 1 <= number <= 65535:
        raise ValueError(f"porta fora do intervalo em {pattern!r}")
    return scheme, host, number


def _split(url: object) -> SplitResult | None:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        return None
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - porta invalida levanta ValueError
    except ValueError:
        return None
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return parts


def is_authorized(url: object, patterns: tuple[str, ...]) -> bool:
    """A URL e http(s), sem credenciais, e casa EXATAMENTE (esquema, host e porta) com alguma origem."""
    parts = _split(url)
    if parts is None:
        return False
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    for pattern in patterns:
        scheme, host, pattern_port = parse_pattern(pattern)
        if scheme == parts.scheme and host == parts.hostname and pattern_port == port:
            return True
    return False


def strip_query(url: str) -> str:
    """URL sem query string, fragmento nem credenciais (e o que e gravado e logado)."""
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else (parts.hostname or "")
    port = parts.port
    netloc = f"{host}:{port}" if port and port != DEFAULT_PORTS[parts.scheme] else host  # porta padrao omitida
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def is_loopback_pattern(pattern: str) -> bool:
    return parse_pattern(pattern)[1] in LOOPBACK_HOSTS


def is_loopback_url(url: object) -> bool:
    parts = _split(url)
    return parts is not None and parts.hostname in LOOPBACK_HOSTS


def origin_of(url: str) -> str:
    """`esquema://host[:porta]` da URL (chave do limite por origem)."""
    parts = urlsplit(strip_query(url))
    return f"{parts.scheme}://{parts.netloc}"


def normalize_target(target: str) -> str:
    """Alvo sem query/fragmento e com o caminho terminando em `/` (comparado como prefixo de caminho)."""
    clean = strip_query(target)
    return clean if clean.endswith("/") else clean + "/"
