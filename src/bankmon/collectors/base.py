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
SOURCE_KINDS = ("html_offline", "browser")

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


SERVICE_STATUSES = ("normal", "warning", "critical", "unknown")
SERVICE_COLORS = ("red", "yellow", "blue", "other")


@dataclass(frozen=True)
class ServiceRecord:
    """Um servico da pagina importada. A geometria SVG ja chega validada (ou None)."""

    position: int
    service: str
    status: str  # um de SERVICE_STATUSES
    status_original: str | None
    color: str | None  # um de SERVICE_COLORS (nunca o stroke cru)
    consistent: bool
    issue: str | None
    institution_id: str | None = None
    svg_viewbox: str | None = None
    svg_width: str | None = None
    svg_height: str | None = None
    svg_path_d: str | None = None


@dataclass(frozen=True)
class SourceInfo:
    """Origem de uma execucao com paginas: arquivo offline (nunca o caminho completo) ou aba do
    navegador (`kind="browser"`, com a URL SEM query string; `file_name` e so um rotulo)."""

    file_name: str
    sha256: str
    size_bytes: int
    captured_at: datetime  # aware, UTC
    kind: str = "html_offline"  # um de SOURCE_KINDS
    url: str | None = None  # so no navegador


@dataclass(frozen=True)
class CollectionResult:
    collector: str
    is_demo: bool
    started_at: datetime  # aware, UTC
    finished_at: datetime  # aware, UTC
    source_status: str  # um de SOURCE_STATUSES
    observations: tuple[Observation, ...] = ()
    error_detail: str | None = None
    services: tuple[ServiceRecord, ...] = ()
    source: SourceInfo | None = None


class Collector(Protocol):
    name: str
    is_demo: bool
    data_kind: str  # demo | offline | browser | live (escolhe o arquivo do banco)

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "Collector":
        """Cria o coletor a partir das opcoes comuns (seed so importa para o demo)."""
        ...

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult: ...
