"""Interpretacao comum de um HTML ja em memoria (arquivo offline ou aba do navegador).

Nada aqui acessa a rede nem o disco: recebe o texto, devolve um `CollectionResult`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from bankmon.collectors import svg_geometry
from bankmon.collectors.base import (
    CollectionResult,
    InstitutionRef,
    Observation,
    ServiceRecord,
    SourceInfo,
)
from bankmon.collectors.dd_parser import _norm, parse_html

MIN_SERVICES = 10
MAX_CHARTS = 1000
LEVEL_BY_STATUS = {"normal": "no_alert", "warning": "attention", "critical": "critical"}
_GEOMETRY = (
    ("viewbox", "svg_viewbox", svg_geometry.validate_viewbox),
    ("width", "svg_width", svg_geometry.validate_dimension),
    ("height", "svg_height", svg_geometry.validate_dimension),
    ("path_d", "svg_path_d", svg_geometry.validate_path_d),
)


def failure_result(collector: str, at: datetime, status: str, detail: str) -> CollectionResult:
    """Resultado sem dados (erro/inacessivel); o horario e o da captura."""
    return CollectionResult(
        collector=collector,
        is_demo=False,
        started_at=at,
        finished_at=at,
        source_status=status,
        error_detail=detail,
    )


def collect_from_html(*args, **kwargs) -> CollectionResult:
    """HTML -> resultado (veja `collect_with_state`; aqui so o resultado)."""
    return collect_with_state(*args, **kwargs)[0]


def collect_with_state(
    html: str,
    institutions: Sequence[InstitutionRef],
    captured_at: datetime,
    source: SourceInfo,
    *,
    collector: str,
    origin_label: str,
) -> tuple[CollectionResult, str]:
    """HTML -> (resultado, page_state do parser: ok | blocked_by_cloudflare | incomplete).

    Pagina de desafio vira `inaccessible`; incompleta ou grande demais vira `error`.
    `origin_label` entra no detalhe das observacoes (ex.: "HTML offline", "aba do navegador").
    O tamanho maximo do HTML e checado pelo chamador (que sabe os bytes originais).
    """
    parsed = parse_html(html, min_services=MIN_SERVICES)
    state = parsed["page_state"]
    if state == "blocked_by_cloudflare":
        return failure_result(
            collector, captured_at, "inaccessible", "blocked_by_cloudflare: o HTML é uma página de desafio"
        ), state
    if parsed["total"] > MAX_CHARTS:
        return failure_result(
            collector, captured_at, "error", f"HTML com {parsed['total']} gráficos (máximo {MAX_CHARTS})"
        ), "incomplete"
    if state != "ok":
        return failure_result(
            collector,
            captured_at,
            "error",
            f"página incompleta: {parsed['total']} serviços (mínimo {MIN_SERVICES})",
        ), state

    services = parsed["services"]
    by_name: dict[str, list[dict]] = {}
    for svc in services:
        if svc["service"]:
            by_name.setdefault(_norm(svc["service"]), []).append(svc)
    institution_by_label: dict[str, str] = {}
    for inst in institutions:
        institution_by_label.setdefault(_norm(inst.dd_label), inst.id)

    result = CollectionResult(
        collector=collector,
        is_demo=False,
        started_at=captured_at,
        finished_at=captured_at,
        source_status="ok",
        observations=tuple(_observations(institutions, by_name, origin_label)),
        services=tuple(_record(i, svc, institution_by_label) for i, svc in enumerate(services)),
        source=source,
    )
    return result, state


def _observations(institutions, by_name, origin_label: str) -> list[Observation]:
    out = []
    for inst in institutions:
        matches = by_name.get(_norm(inst.dd_label))
        if not matches:
            out.append(Observation(inst.id, "not_listed"))
            continue
        statuses = {m["status"] for m in matches}
        if len(statuses) != 1 or "unknown" in statuses:
            continue  # inconsistente/duplicado em conflito: sem observacao ("Sem dados")
        first = matches[0]
        out.append(
            Observation(
                inst.id,
                LEVEL_BY_STATUS[first["status"]],
                f"Downdetector ({origin_label}): {first['status_original']}",
            )
        )
    return out


def _record(position: int, svc: dict, institution_by_label: dict[str, str]) -> ServiceRecord:
    issues = [svc["issue"]] if svc["issue"] else []
    geometry: dict[str, str | None] = {}
    for raw_key, column, validate in _GEOMETRY:
        raw = svc["svg"][raw_key]
        geometry[column] = validate(raw) if raw is not None else None
        if raw is not None and geometry[column] is None:
            issues.append(f"{column}_invalido")
    name = svc["service"]
    return ServiceRecord(
        position=position,
        service=name or "(sem nome)",
        status=svc["status"],
        status_original=svc["status_original"],
        color=svc["color"],
        consistent=svc["consistent"],
        issue="; ".join(issues) or None,
        institution_id=institution_by_label.get(_norm(name)) if name else None,
        **geometry,
    )
