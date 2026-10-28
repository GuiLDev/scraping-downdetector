"""Stub do coletor do Downdetector (Etapa 2).

Este modulo NAO faz acesso de rede e nao deve importar bibliotecas de rede.

O Downdetector protege o site com Cloudflare (desafio/CAPTCHA). Contornar essa
protecao (reaproveitar `cf_clearance`, navegador headless para evasao, imitar
fingerprint, proxy rotativo etc.) e proibido neste projeto. Somente um canal
autorizado, a API Enterprise ou permissao por escrito da Ookla/Downdetector,
pode ser implementado aqui. Enquanto isso nao existir, cada execucao registra
`not_authorized` e o dashboard mostra todas as instituicoes como "Sem dados".
"""

from __future__ import annotations

from typing import Sequence

from bankmon.collectors.base import (
    Clock,
    CollectionResult,
    InstitutionRef,
)
from bankmon.timeutil import utc_now

PENDING_MESSAGE = "Coleta automatizada pendente de autorização do Downdetector"


class DowndetectorCollector:
    name = "downdetector"
    is_demo = False

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "DowndetectorCollector":
        return cls(clock=clock)

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult:
        now = self._clock()
        return CollectionResult(
            collector=self.name,
            is_demo=self.is_demo,
            started_at=now,
            finished_at=now,
            source_status="not_authorized",
            observations=(),
            error_detail=PENDING_MESSAGE,
        )
