"""Saneamento de texto vindo de excecoes/coletores antes de gravar ou exibir."""

from __future__ import annotations

import re
import unicodedata

MAX_LEN = 300
# A entrada e truncada ANTES das regex (limita o custo e evita ReDoS).
MAX_INPUT = 2000

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_SCHEME = r"[a-z][a-z0-9+.-]{0,31}://"
# [^\s/]*@ e guloso: senha contendo '@' tambem e removida (ate o ultimo '@' da autoridade).
_URL_USERINFO = re.compile(rf"(?i)\b({_SCHEME})[^\s/]*@")
_URL_QUERY = re.compile(rf"(?i)\b({_SCHEME}[^\s?#]*)[?#]\S*")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)[\"']?\s*[:=].*")
_SECRET_KEY = (
    r"(?:[\w-]{0,40}(?:token|secret|key|password|passwd|pwd|pass|session|sessionid"
    r"|credentials?|auth)"
    r"|authorization|cf_clearance)"
)
_JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_SECRET_PAIR = re.compile(
    rf"(?i)(?P<k>{_SECRET_KEY})[\"']?\s*[:=]\s*[\"']?"
    r"(?:(?:bearer|basic|token)\s+)?[^\s&;,\"'}\])]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
# "basic" so conta como credencial se o token parecer base64 (digito, '=', '+' ou '/').
_BASIC = re.compile(r"(?i)\bbasic\s+(?=[A-Za-z0-9+/]*[\d=+/])[A-Za-z0-9+/]{6,}={0,2}")


def _strip_invisible(text: str) -> str:
    """Controles, Zl e Zp viram espaco; Cf (inclui bidi U+202E) e removido."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append(" " if cat in ("Cc", "Zl", "Zp") else ch)
    return "".join(out)


def safe_text(text: str, max_len: int = MAX_LEN) -> str:
    """Remove credenciais/query strings, ANSI, controles e Unicode invisivel; trunca."""
    raw = str(text)
    text = raw[:MAX_INPUT]
    if len(raw) > MAX_INPUT:
        # O corte pode cair no meio de uma credencial (ex.: "user:SEN"): descarta o token parcial.
        text = re.sub(r"\S*$", "", text)
    text = _ANSI.sub("", text)
    text = _strip_invisible(text)
    text = _URL_USERINFO.sub(r"\1", text)
    text = _URL_QUERY.sub(r"\1", text)
    text = _COOKIE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _SECRET_PAIR.sub(lambda m: f"{m.group('k')}=***", text)
    text = _JWT.sub("***", text)
    text = _BEARER.sub("bearer ***", text)
    text = _BASIC.sub("basic ***", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def safe_error(exc: BaseException) -> str:
    return safe_text(f"{type(exc).__name__}: {exc}")
