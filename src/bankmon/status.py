"""Regras de estado (funcoes puras; `now` sempre injetado).

Estados efetivos, calculados na leitura:
  critical | attention | no_alert | no_data | not_monitored

Regras:
  1. Se a execucao mais recente (qualquer status) nao for `ok` -> tudo `no_data`.
  2. Se nao ha execucao ou now - finished_at > stale_after -> `no_data`
     (exatamente stale_after ainda vale).
  3. Senao usa a observacao da instituicao: ausente -> `no_data`;
     `not_listed` (configurada, mas fora da pagina da fonte) -> `no_data`, nunca normalidade;
     demais niveis mapeados diretamente. `not_monitored` fica reservado para o que esta fora
     do escopo de monitoramento, nao para ausencia na pagina.
  4. Nunca `no_alert` sem execucao valida.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Sequence

CRITICAL = "critical"
ATTENTION = "attention"
NO_ALERT = "no_alert"
NO_DATA = "no_data"
NOT_MONITORED = "not_monitored"
STATES = (CRITICAL, ATTENTION, NO_ALERT, NO_DATA, NOT_MONITORED)

SEVERITY = {CRITICAL: 0, ATTENTION: 1, NO_ALERT: 2, NO_DATA: 3, NOT_MONITORED: 4}
LEVEL_TO_STATE = {
    "critical": CRITICAL,
    "attention": ATTENTION,
    "no_alert": NO_ALERT,
    "not_listed": NO_DATA,  # ausente da pagina: sem dados (o fato bruto fica em observations)
}

SECTION_ALERTS = "alerts"
SECTION_CLEAR = "clear"
SECTION_NO_COVERAGE = "no_coverage"
SECTION_TITLES = {
    SECTION_ALERTS: "Com alerta",
    SECTION_CLEAR: "Sem alerta identificado",
    SECTION_NO_COVERAGE: "Sem cobertura",
}
STATE_SECTION = {
    CRITICAL: SECTION_ALERTS,
    ATTENTION: SECTION_ALERTS,
    NO_ALERT: SECTION_CLEAR,
    NO_DATA: SECTION_NO_COVERAGE,
    NOT_MONITORED: SECTION_NO_COVERAGE,
}


@dataclass(frozen=True)
class RunView:
    """Visao de uma execucao para o calculo de estados."""

    run_id: int
    finished_at: datetime
    source_status: str
    is_demo: bool
    levels: Mapping[str, str]  # institution_id -> level
    error_detail: str | None = None


@dataclass(frozen=True)
class InstitutionInfo:
    id: str
    name: str
    category_id: str
    category_name: str
    category_order: int


@dataclass(frozen=True)
class Card:
    institution_id: str
    name: str
    category_id: str
    category_name: str
    category_order: int
    state: str


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    cards: tuple[Card, ...]


def effective_state(
    institution_id: str,
    latest_run: RunView | None,
    now: datetime,
    stale_after: timedelta,
) -> str:
    """Estado de uma instituicao em `now`. `latest_run` e a execucao mais recente de todas."""
    if latest_run is None or latest_run.source_status != "ok":
        return NO_DATA
    if now - latest_run.finished_at > stale_after:
        return NO_DATA
    level = latest_run.levels.get(institution_id)
    return LEVEL_TO_STATE.get(level, NO_DATA) if level is not None else NO_DATA


def build_cards(
    institutions: Iterable[InstitutionInfo],
    latest_run: RunView | None,
    now: datetime,
    stale_after: timedelta,
) -> list[Card]:
    return [
        Card(
            i.id,
            i.name,
            i.category_id,
            i.category_name,
            i.category_order,
            effective_state(i.id, latest_run, now, stale_after),
        )
        for i in institutions
    ]


def _sort_key(card: Card) -> tuple[int, int, str]:
    return (SEVERITY[card.state], card.category_order, card.name.casefold())


def group_and_sort(cards: Iterable[Card]) -> list[Section]:
    """Tres secoes sempre presentes (mesmo vazias), na ordem do dashboard."""
    buckets: dict[str, list[Card]] = {k: [] for k in SECTION_TITLES}
    for card in cards:
        buckets[STATE_SECTION[card.state]].append(card)
    return [
        Section(key, SECTION_TITLES[key], tuple(sorted(buckets[key], key=_sort_key)))
        for key in (SECTION_ALERTS, SECTION_CLEAR, SECTION_NO_COVERAGE)
    ]


# --- Metricas para graficos -------------------------------------------------


@dataclass(frozen=True)
class HourBucket:
    hour: datetime  # inicio da hora (UTC)
    attention: int
    critical: int
    has_data: bool  # False se nao houve execucao ok na hora


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def hourly_alert_counts(
    runs: Sequence[RunView],
    start: datetime,
    end: datetime,
    institution_ids: Iterable[str] | None = None,
) -> list[HourBucket]:
    """Instituicoes em attention/critical por hora em [start, end).

    Em cada hora vale a ultima execucao `ok`; horas sem execucao ok vem com
    has_data=False e contagens 0 (nao significam "sem alerta").
    """
    allowed = set(institution_ids) if institution_ids is not None else None
    last_ok: dict[datetime, RunView] = {}
    for run in sorted(runs, key=lambda r: r.finished_at):
        if run.source_status == "ok":
            last_ok[_floor_hour(run.finished_at)] = run

    buckets: list[HourBucket] = []
    hour = _floor_hour(start)
    while hour < end:
        run = last_ok.get(hour)
        if run is None:
            buckets.append(HourBucket(hour, 0, 0, False))
        else:
            levels = [
                lvl
                for iid, lvl in run.levels.items()
                if allowed is None or iid in allowed
            ]
            buckets.append(
                HourBucket(hour, levels.count("attention"), levels.count("critical"), True)
            )
        hour += timedelta(hours=1)
    return buckets


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    state: str


def state_timeline(
    institution_id: str,
    runs: Sequence[RunView],
    start: datetime,
    end: datetime,
    stale_after: timedelta,
) -> list[Segment]:
    """Linha do tempo de estados em [start, end), com lacunas como `no_data`.

    Consistente com `effective_state`: apos cada execucao o estado vale ate
    finished_at + stale_after (inclusive) ou a proxima execucao; depois disso e
    `no_data`. Passe tambem a ultima execucao anterior a `start`.
    """
    segments: list[Segment] = []

    def add(seg_start: datetime, seg_end: datetime, state: str) -> None:
        seg_start, seg_end = max(seg_start, start), min(seg_end, end)
        if seg_start >= seg_end:
            return
        if segments and segments[-1].state == state and segments[-1].end == seg_start:
            segments[-1] = Segment(segments[-1].start, seg_end, state)
        else:
            segments.append(Segment(seg_start, seg_end, state))

    ordered = sorted(runs, key=lambda r: r.finished_at)
    if not ordered:
        add(start, end, NO_DATA)
        return segments

    add(start, ordered[0].finished_at, NO_DATA)
    for idx, run in enumerate(ordered):
        next_at = ordered[idx + 1].finished_at if idx + 1 < len(ordered) else end
        state = effective_state(institution_id, run, run.finished_at, stale_after)
        if state == NO_DATA:
            add(run.finished_at, next_at, NO_DATA)
        else:
            valid_until = min(next_at, run.finished_at + stale_after)
            add(run.finished_at, valid_until, state)
            add(valid_until, next_at, NO_DATA)
    return segments
