"""Coletor OFFLINE: le um HTML do Downdetector exportado manualmente (sem rede).

Nada aqui acessa a internet: so le um arquivo local. Os dados sao historicos (validacao): o horario
registrado e o da captura do arquivo, nao o da importacao. A interpretacao do HTML e compartilhada
com o coletor do navegador (`html_common`); veja `dd_parser` para o parser.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Sequence

from bankmon.collectors.base import Clock, CollectionResult, InstitutionRef, SourceInfo
from bankmon.collectors.dd_parser import MAX_BYTES
from bankmon.collectors.html_common import collect_from_html, failure_result
from bankmon.timeutil import ensure_utc


class OfflineHtmlCollector:
    name = "offline_html"
    is_demo = False
    data_kind = "offline"

    def __init__(self, path: Path | str, captured_at: datetime) -> None:
        self._path = Path(path)
        self._captured_at = ensure_utc(captured_at)

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "OfflineHtmlCollector":
        raise ValueError(
            "o coletor offline_html exige um arquivo: use `python -m bankmon import-html ARQUIVO`"
        )

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult:
        try:
            with self._path.open("rb") as fh:
                data = fh.read(MAX_BYTES + 1)
        except OSError:
            return failure_result(self.name, self._captured_at, "error", "arquivo HTML ilegível ou inexistente")
        if len(data) > MAX_BYTES:
            return failure_result(
                self.name,
                self._captured_at,
                "error",
                f"arquivo maior que {MAX_BYTES // (1024 * 1024)} MB: recusado",
            )
        source = SourceInfo(
            file_name=self._path.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            captured_at=self._captured_at,
        )
        return collect_from_html(
            data.decode("utf-8", errors="replace"),
            institutions,
            self._captured_at,
            source,
            collector=self.name,
            origin_label="HTML offline",
        )
