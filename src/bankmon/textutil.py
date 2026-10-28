"""Saneamento de texto vindo de excecoes/coletores antes de gravar ou exibir."""

from __future__ import annotations

import re

MAX_LEN = 300

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]*@")
_URL_QUERY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#]*)[?#]\S*")
_SECRET_PAIR = re.compile(
    r"(?i)\b(token|access_token|api[_-]?key|apikey|key|password|passwd|pwd|secret|"
    r"auth|authorization|session|sessionid|cf_clearance)\s*[=:]\s*(?:bearer\s+)?[^\s&;,]+"
)


def safe_text(text: str, max_len: int = MAX_LEN) -> str:
    """Remove credenciais/query strings de URLs, ANSI e controles; trunca."""
    text = _ANSI.sub("", text)
    text = _URL_USERINFO.sub(r"\1", text)
    text = _URL_QUERY.sub(r"\1", text)
    text = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}=***", text)
    text = _CONTROL.sub(" ", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def safe_error(exc: BaseException) -> str:
    return safe_text(f"{type(exc).__name__}: {exc}")
