"""Contrato dos coletores.

Plugar um coletor novo = criar uma classe que satisfaca `Collector` e registra-la
em `registry.py`. Dashboard e banco nao mudam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol, Sequence

LEVELS = ("no_alert", "attention", "critical", "not_listed")
SOURCE_STATUSES = ("ok", "inaccessible", "error", "not_authorized")

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class InstitutionRef:
    """Dados da instituicao que o coletor precisa para consultar a fonte."""

    id: str
    name: str
    dd_label: str


@dataclass(frozen=True)
class Observation:
    institution_id: str
    level: str  # um de LEVELS
    detail: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    collector: str
    is_demo: bool
    started_at: datetime  # aware, UTC
    finished_at: datetime  # aware, UTC
    source_status: str  # um de SOURCE_STATUSES
    observations: tuple[Observation, ...] = ()
    error_detail: str | None = None


class Collector(Protocol):
    name: str
    is_demo: bool

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "Collector":
        """Cria o coletor a partir das opcoes comuns (seed so importa para o demo)."""
        ...

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult: ...
