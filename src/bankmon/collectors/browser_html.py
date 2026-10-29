"""Coletor da ABA DO NAVEGADOR: recebe o HTML que a extensao leu da aba ja aberta pelo usuario.

Este modulo nao faz rede: so interpreta o texto recebido (`html_common`). O horario registrado e o
da leitura (relogio do servidor). A URL guardada nunca tem query string (ver `bankmon.origins`).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Sequence

from bankmon.collectors.base import Clock, CollectionResult, InstitutionRef, SourceInfo
from bankmon.collectors.dd_parser import MAX_BYTES
from bankmon.collectors.html_common import collect_with_state, failure_result
from bankmon.timeutil import ensure_utc

SOURCE_LABEL = "aba-navegador"  # `file_name` da origem: rotulo fixo, sem caminho


class BrowserHtmlCollector:
    name = "navegador"
    is_demo = False
    data_kind = "browser"

    def __init__(self, html: str, url: str, captured_at: datetime) -> None:
        self._html = html
        self._url = url
        self._captured_at = ensure_utc(captured_at)
        # Preenchido por `collect`: ok | blocked_by_cloudflare | incomplete | error
        self.page_state = "error"

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "BrowserHtmlCollector":
        raise ValueError(
            "o coletor navegador exige o HTML da aba: use `python -m bankmon serve --navegador`"
        )

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult:
        data = self._html.encode("utf-8", errors="replace")
        if len(data) > MAX_BYTES:
            return failure_result(
                self.name,
                self._captured_at,
                "error",
                f"HTML maior que {MAX_BYTES // (1024 * 1024)} MB: recusado",
            )
        source = SourceInfo(
            file_name=SOURCE_LABEL,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            captured_at=self._captured_at,
            kind="browser",
            url=self._url,
        )
        result, self.page_state = collect_with_state(
            self._html,
            institutions,
            self._captured_at,
            source,
            collector=self.name,
            origin_label="aba do navegador",
        )
        return result
