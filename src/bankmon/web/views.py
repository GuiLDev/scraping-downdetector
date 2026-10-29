"""Montagem dos dados exibidos pelas rotas (somente leitura).

Nenhuma regra de estado vive aqui: os estados vem de `bankmon.status`. Este modulo
so le o banco, aplica filtros, formata horarios (America/Sao_Paulo) e escolhe rotulos.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby
from typing import Callable

from bankmon import db, status
from bankmon.browser_service import (
    FAILURES_REASON,
    FAILURES_TO_PAUSE,
    EXTERNAL_FAILURES_REASON,
    NO_RESUME_REASONS,
    SUSPENDED_REASON,
)
from bankmon.collectors import svg_geometry
from bankmon.config import Config
from bankmon.status import InstitutionInfo, RunView
from bankmon.textutil import safe_error, safe_text
from bankmon.timeutil import to_local

log = logging.getLogger("bankmon.web")

PERIODS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_PERIOD = "24h"
MAX_QUERY_LEN = 60

# rotulo, icone (decorativo, aria-hidden; U+FE0E forca glifo de texto, sem emoji colorido), classe CSS
STATE_UI: dict[str, tuple[str, str, str]] = {
    status.CRITICAL: ("Crítico", "✖︎", "state-critical"),
    status.ATTENTION: ("Atenção", "▲", "state-attention"),
    status.NO_ALERT: ("Sem alerta", "✔︎", "state-no-alert"),
    status.NO_DATA: ("Sem dados", "?", "state-no-data"),
    status.NOT_MONITORED: ("Não monitorado", "–", "state-not-monitored"),
}
SOURCE_STATUS_LABELS = {
    "inaccessible": "Downdetector inacessível",
    "not_authorized": "Coletor pendente de autorização",
    "error": "Falha na coleta",
}
DISCLAIMER = (
    "Indicadores segundo o Downdetector; não são confirmação oficial de indisponibilidade."
)
DEMO_BANNER = "DADOS DE DEMONSTRAÇÃO — não refletem o Downdetector"


OFFLINE_BANNER = (
    "DADOS HISTÓRICOS DE VALIDAÇÃO OFFLINE — HTML exportado manualmente do Downdetector{captured}. "
    "Não é monitoramento ao vivo."
)
HISTORICAL = "captura histórica"


BROWSER_BANNER_SIMULATION = (
    "COLETA PELO NAVEGADOR — SIMULAÇÃO LOCAL (páginas de teste, não é o Downdetector)"
)
BROWSER_BANNER_EXTERNAL = "COLETA PELO NAVEGADOR — leitura da aba aberta pelo usuário (origem autorizada)"
PAUSED_NOTICE = (
    "Coleta pausada: bloqueio detectado às {at}. Resolva manualmente na aba do navegador e clique em "
    "Retomar verificação. Os dados exibidos estão desatualizados."
)
FAILURES_NOTICE = (
    "Coleta pausada: {n} leituras seguidas sem sucesso desde {at}. Verifique a aba do navegador e clique "
    "em Retomar verificação. Os dados exibidos estão desatualizados."
)
EXTERNAL_FAILURES_NOTICE = (
    "Coleta suspensa: {n} leituras seguidas sem sucesso em origem externa. A coleta só volta após "
    "revisão da configuração e da autorização."
)
SUSPENDED_NOTICE = (
    "Coleta suspensa: a fonte exibiu uma verificação. A coleta automática fica encerrada até revisão "
    "da autorização."
)
VERIFYING_NOTICE = "Verificação solicitada; aguardando a próxima leitura da aba."


def offline_banner(cfg: Config, snap: "Snapshot | None") -> str | None:
    """Texto da faixa do modo (offline ou navegador; None nos demais). Texto puro, autoescapado."""
    if cfg.is_browser:
        return BROWSER_BANNER_EXTERNAL if cfg.browser_external else BROWSER_BANNER_SIMULATION
    if not cfg.is_offline:
        return None
    run = None if snap is None else (snap.last_ok or snap.latest)
    captured = f", capturado em {dm_hm(run.finished_at)}" if run is not None else ""
    return OFFLINE_BANNER.format(captured=captured)


def hm(dt: datetime) -> str:
    return to_local(dt).strftime("%H:%M")


def dm_hm(dt: datetime) -> str:
    return to_local(dt).strftime("%d/%m %H:%M")


@dataclass(frozen=True)
class Notice:
    kind: str  # css: source | stale | info
    text: str
    detail: str | None = None


@dataclass
class Snapshot:
    """Leitura do banco: `db_state` e ok | missing | error."""

    db_state: str = "ok"
    latest: RunView | None = None
    last_ok: RunView | None = None
    last_transitions: dict[str, db.TransitionRow] = field(default_factory=dict)
    # institution_id -> grafico ja revalidado (so da execucao `last_ok`; vazio se nao houver)
    charts: dict[str, dict] = field(default_factory=dict)
    coleta: db.CollectorState | None = None  # so no modo navegador


def infos_from_config(cfg: Config) -> list[InstitutionInfo]:
    """Instituicoes habilitadas segundo o TOML (fonte da verdade para exibicao).

    Assim uma instituicao adicionada ao TOML aparece como "Sem dados" mesmo antes de
    o banco ser sincronizado, e o dashboard nunca precisa escrever no banco.
    """
    cats = {c.id: c for c in cfg.categories}
    return [
        InstitutionInfo(i.id, i.name, i.category, cats[i.category].name, cats[i.category].order)
        for i in cfg.institutions
        if i.enabled
    ]


def read_db(cfg: Config, reader: Callable[[sqlite3.Connection], object]):
    """Executa `reader(conn)` numa conexao somente leitura.

    Retorna (db_state, resultado). Banco ausente ou ilegivel nunca levanta: o
    chamador deve tratar `db_state != "ok"` como "Sem dados".
    """
    if not cfg.db_path.exists():
        return "missing", None
    try:
        conn = db.open_readonly(cfg.db_path)
    except sqlite3.Error as exc:
        log.error("Falha ao abrir o banco em modo leitura: %s", safe_error(exc))
        return "error", None
    try:
        return "ok", reader(conn)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        # Dado malformado num banco valido (timestamp invalido, estado desconhecido):
        # trata como ilegivel (tudo "Sem dados" + aviso), nunca como verde nem 500.
        log.error("Falha ao consultar/interpretar o banco: %s", safe_error(exc))
        return "error", None
    finally:
        conn.close()


def _checked(transitions):
    """Valida estados das transicoes dentro do `reader` (estado desconhecido -> KeyError)."""
    for t in transitions:
        STATE_UI[t.from_state], STATE_UI[t.to_state]  # noqa: B018
    return transitions


CHART_COLORS = ("red", "yellow", "blue")


def _service_chart(row: sqlite3.Row) -> dict | None:
    """Grafico de 24 h de um servico gravado, ou None. Revalida tudo antes de usar."""
    if not row["consistent"] or row["color"] not in CHART_COLORS:
        return None
    viewbox = svg_geometry.validate_viewbox(row["svg_viewbox"])
    path_d = svg_geometry.validate_path_d(row["svg_path_d"])
    if viewbox is None or path_d is None:
        return None
    for dim in (row["svg_width"], row["svg_height"]):
        if dim is not None and svg_geometry.validate_dimension(dim) is None:
            return None
    return {"color": row["color"], "viewbox": viewbox, "d": path_d}


def _load_charts(conn: sqlite3.Connection, latest, last_ok) -> dict[str, dict]:
    # So quando a execucao dos estados e a ultima valida (senao o grafico contradiria o cartao).
    if last_ok is None or latest is None or latest.run_id != last_ok.run_id:
        return {}
    charts: dict[str, dict] = {}
    try:
        rows = db.run_services(conn, last_ok.run_id)
    except sqlite3.Error:  # banco antigo (schema v1) sem run_services: sem graficos, nada mais
        return {}
    for row in rows:
        iid = row["institution_id"]
        if iid is None or iid in charts:
            continue
        chart = _service_chart(row)
        if chart is not None:
            charts[iid] = chart
    return charts


def load_snapshot(cfg: Config) -> Snapshot:
    def reader(conn: sqlite3.Connection) -> Snapshot:
        last_transitions = db.last_transition_by_institution(conn)
        _checked(last_transitions.values())
        latest, last_ok = db.latest_run(conn), db.latest_ok_run(conn)
        return Snapshot(
            latest=latest,
            last_ok=last_ok,
            last_transitions=last_transitions,
            charts=_load_charts(conn, latest, last_ok),
            coleta=db.get_collector_state(conn) if cfg.is_browser else None,
        )

    state, snap = read_db(cfg, reader)
    if snap is None:
        return Snapshot(db_state=state)
    return snap


def is_demo(cfg: Config, latest: RunView | None) -> bool:
    return cfg.is_demo or (latest is not None and latest.is_demo)


def build_notices(
    cfg: Config, snap: Snapshot, now: datetime
) -> tuple[list[Notice], dict[str, str]]:
    """Avisos da fonte/verificacao e o resumo de cabecalho (ultima/proxima verificacao)."""
    notices: list[Notice] = []
    header = {"last_ok": "nenhuma", "next": "indefinida"}
    stale_after = timedelta(minutes=cfg.stale_after_minutes)

    if snap.db_state == "missing":
        notices.append(
            Notice(
                "info",
                "Banco de dados ainda não criado. Rode `python -m bankmon init-db` e "
                "`python -m bankmon collect` para gerar a primeira verificação.",
            )
        )
    elif snap.db_state == "error":
        notices.append(Notice("source", "Banco de dados ilegível ou incompatível; veja os logs."))

    latest, last_ok = snap.latest, snap.last_ok
    if latest is not None and latest.source_status != "ok":
        at = hm(latest.finished_at)
        text = {
            "inaccessible": f"Downdetector indisponível às {at}",
            "not_authorized": "Coletor Downdetector pendente de autorização",
            "error": f"Falha na última coleta às {at}",
        }.get(latest.source_status, f"Fonte com problema às {at}")
        detail = safe_text(latest.error_detail) if latest.error_detail else None
        notices.append(Notice("source", text, detail))

    if last_ok is not None and cfg.is_offline:
        header["last_ok"] = f"{dm_hm(last_ok.finished_at)} ({HISTORICAL})"
    elif last_ok is not None:
        minutes = max(0, int((now - last_ok.finished_at).total_seconds() // 60))
        header["last_ok"] = f"{dm_hm(last_ok.finished_at)} (há {minutes} min)"
        if now - last_ok.finished_at > stale_after:
            since = last_ok.finished_at + stale_after
            notices.append(Notice("stale", f"Verificação vencida desde {hm(since)}"))
    elif snap.db_state == "ok":
        notices.append(Notice("stale", "Nenhuma verificação válida registrada até agora."))

    if cfg.is_offline:
        header["next"] = HISTORICAL
    elif latest is not None:
        nxt = latest.finished_at + timedelta(minutes=cfg.interval_minutes)
        header["next"] = hm(nxt) + (" (atrasada)" if nxt < now else "")
    return notices, header


# --- Pagina inicial -----------------------------------------------------------


def parse_filters(args, cfg: Config) -> dict[str, str]:
    """Whitelist: valor invalido vira vazio (ignorado), nunca erro."""
    category = args.get("category", "")
    state = args.get("state", "")
    q = " ".join(args.get("q", "").split())[:MAX_QUERY_LEN]
    return {
        "category": category if category in {c.id for c in cfg.categories} else "",
        "state": state if state in status.STATES else "",
        "q": q,
    }


def since_text(cfg: Config, card: status.Card, snap: Snapshot, now: datetime) -> str | None:
    """"desde ..." (ao vivo/demo) ou "Observado em: ..." (offline: so se sabe quando foi visto)."""
    if cfg.is_offline:
        if snap.last_ok is None or card.state in (status.NO_DATA, status.NOT_MONITORED):
            return None
        local = to_local(snap.last_ok.finished_at)
        return f"Observado em: {local:%d/%m} às {local:%H:%M}"
    tr = snap.last_transitions.get(card.institution_id)
    if tr is not None and tr.to_state == card.state:
        same_day = to_local(tr.changed_at).date() == to_local(now).date()
        return "desde " + (hm(tr.changed_at) if same_day else dm_hm(tr.changed_at))
    return None


def stale_reading_text(cfg: Config, card: status.Card, snap: Snapshot) -> str | None:
    """Modo navegador: cartao "Sem dados" mostra a ultima leitura valida, JAMAIS como estado atual.

    So texto (sem icone nem classe de estado): dado velho nunca aparece como verde.
    """
    if not cfg.is_browser or card.state != status.NO_DATA or snap.last_ok is None:
        return None
    ok = snap.last_ok
    previous = status.effective_state(
        card.institution_id, ok, ok.finished_at, timedelta(minutes=cfg.stale_after_minutes)
    )
    if previous in (status.NO_DATA, status.NOT_MONITORED):
        return None
    local = to_local(ok.finished_at)
    return (
        f"Última leitura válida (desatualizada): {STATE_UI[previous][0]} "
        f"em {local:%d/%m} às {local:%H:%M}"
    )


def coleta_notice(cfg: Config, snap: Snapshot) -> Notice | None:
    """Aviso de pausa/verificacao da coleta pelo navegador."""
    if not cfg.is_browser or snap.coleta is None:
        return None
    if snap.coleta.estado == "pausado":
        at = hm(snap.coleta.desde) if snap.coleta.desde else "--:--"
        if snap.coleta.motivo == SUSPENDED_REASON:
            return Notice("source", SUSPENDED_NOTICE)
        if snap.coleta.motivo == FAILURES_REASON:
            return Notice("source", FAILURES_NOTICE.format(n=FAILURES_TO_PAUSE, at=at))
        if snap.coleta.motivo == EXTERNAL_FAILURES_REASON:
            return Notice("source", EXTERNAL_FAILURES_NOTICE.format(n=FAILURES_TO_PAUSE))
        return Notice("source", PAUSED_NOTICE.format(at=at))
    if snap.coleta.estado == "verificando":
        return Notice("info", VERIFYING_NOTICE)
    return None


def _card_view(cfg: Config, card: status.Card, snap: Snapshot, now: datetime, demo: bool) -> dict:
    label, icon, css = STATE_UI[card.state]
    since = since_text(cfg, card, snap, now)
    return {
        "id": card.institution_id,
        "name": card.name,
        "category": card.category_name,
        "state": card.state,
        "label": label,
        "icon": icon,
        "css": css,
        "since": since,
        "stale_reading": stale_reading_text(cfg, card, snap),
        "chart": snap.charts.get(card.institution_id),
        "demo": demo,
    }


def build_index(cfg: Config, args, now: datetime) -> dict:
    snap = load_snapshot(cfg)
    stale_after = timedelta(minutes=cfg.stale_after_minutes)
    demo = is_demo(cfg, snap.latest)
    filters = parse_filters(args, cfg)

    infos = infos_from_config(cfg)
    # Sem banco legivel nao ha execucao: build_cards devolve tudo `no_data`.
    latest = snap.latest if snap.db_state == "ok" else None
    cards = status.build_cards(infos, latest, now, stale_after)
    counts = {s: sum(1 for c in cards if c.state == s) for s in status.STATES}

    needle = filters["q"].casefold()
    visible = [
        c
        for c in cards
        if (not filters["category"] or c.category_id == filters["category"])
        and (not filters["state"] or c.state == filters["state"])
        and (not needle or needle in c.name.casefold())
    ]
    sections = []
    for section in status.group_and_sort(visible):
        blocks = []
        # Ordem de group_and_sort preservada: dentro de cada estado os cartoes ja
        # vem contiguos por categoria.
        for (state, _cat), group in groupby(section.cards, key=lambda c: (c.state, c.category_id)):
            group = list(group)
            blocks.append(
                {
                    "category": group[0].category_name,
                    "state_label": STATE_UI[state][0],
                    "cards": [_card_view(cfg, c, snap, now, demo) for c in group],
                }
            )
        sections.append(
            {"key": section.key, "title": section.title, "count": len(section.cards), "blocks": blocks}
        )

    notices, header = build_notices(cfg, snap, now)
    pause = coleta_notice(cfg, snap)
    if pause is not None:
        notices.insert(0, pause)
    return {
        "demo": demo,
        "offline_banner": offline_banner(cfg, snap),
        "notices": notices,
        "coleta_paused": snap.coleta is not None and snap.coleta.estado == "pausado"
        and snap.coleta.motivo not in NO_RESUME_REASONS,  # suspensa (externa): sem botao
        "header": header,
        "sections": sections,
        "filters": filters,
        "category_options": [("", "Todas")] + [(c.id, c.name) for c in cfg.categories],
        "state_options": [("", "Todos")] + [(s, STATE_UI[s][0]) for s in status.STATES],
        "counts": [(STATE_UI[s][0], STATE_UI[s][2], counts[s]) for s in status.STATES],
    }


# --- Historico ----------------------------------------------------------------


def parse_period(args) -> str:
    value = args.get("period", DEFAULT_PERIOD)
    return value if value in PERIODS else DEFAULT_PERIOD


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _transition_view(t: db.TransitionRow, names: dict[str, str]) -> dict:
    return {
        "when": dm_hm(t.changed_at),
        "id": t.institution_id,
        "name": names.get(t.institution_id, t.institution_id),
        "from_label": STATE_UI[t.from_state][0],
        "from_icon": STATE_UI[t.from_state][1],
        "from_css": STATE_UI[t.from_state][2],
        "to_label": STATE_UI[t.to_state][0],
        "to_icon": STATE_UI[t.to_state][1],
        "to_css": STATE_UI[t.to_state][2],
    }


def build_history(cfg: Config, args, now: datetime) -> dict:
    period = parse_period(args)
    start = _floor_hour(now - PERIODS[period])
    infos = infos_from_config(cfg)
    names = {i.id: i.name for i in infos}

    def reader(conn: sqlite3.Connection):
        return (
            db.latest_run(conn),
            db.latest_ok_run(conn),
            db.runs_between(conn, start, now),
            _checked(db.transitions_between(conn, start, now)),
            db.failed_runs_between(conn, start, now),
        )

    db_state, data = read_db(cfg, reader)
    latest, last_ok, runs, transitions, failed = (
        data if data is not None else (None, None, [], [], [])
    )
    demo = is_demo(cfg, latest) or any(r.is_demo for r in runs)

    buckets = status.hourly_alert_counts(runs, start, now, [i.id for i in infos])
    chart = {
        "demo": demo,
        "total": len(infos),
        "hours": [to_local(b.hour).strftime("%d/%m %H:%M") for b in buckets],
        "has_data": [b.has_data for b in buckets],
        "attention": [b.attention for b in buckets],
        "critical": [b.critical for b in buckets],
        "labels": {
            "attention": STATE_UI[status.ATTENTION][0],
            "critical": STATE_UI[status.CRITICAL][0],
            "no_data": STATE_UI[status.NO_DATA][0],
        },
    }
    notices, _ = build_notices(cfg, Snapshot(db_state, latest, last_ok), now)
    return {
        "demo": demo,
        "offline_banner": offline_banner(cfg, Snapshot(db_state, latest, last_ok)),
        "notices": notices,
        "period": period,
        "periods": list(PERIODS),
        "chart": chart,
        "no_hours_with_data": not any(chart["has_data"]),
        "transitions": [_transition_view(t, names) for t in transitions],
        "failed": [
            {
                "when": dm_hm(f.finished_at),
                "collector": f.collector,
                "status": SOURCE_STATUS_LABELS.get(f.source_status, f.source_status),
                "detail": safe_text(f.error_detail) if f.error_detail else "",
                "demo": f.is_demo,
            }
            for f in failed
        ],
    }


# --- Instituicao --------------------------------------------------------------


def build_institution(cfg: Config, institution_id: str, args, now: datetime) -> dict | None:
    info = next((i for i in infos_from_config(cfg) if i.id == institution_id), None)
    if info is None:
        return None
    period = parse_period(args)
    start = now - PERIODS[period]
    stale_after = timedelta(minutes=cfg.stale_after_minutes)

    def reader(conn: sqlite3.Connection):
        before = db.run_before(conn, start)
        runs = db.runs_between(conn, start, now)
        return (
            db.latest_run(conn),
            db.latest_ok_run(conn),
            ([before] if before else []) + runs,
            _checked(db.transitions_between(conn, start, now, institution_id=institution_id)),
        )

    db_state, data = read_db(cfg, reader)
    latest, last_ok, runs, transitions = data if data is not None else (None, None, [], [])
    demo = is_demo(cfg, latest) or any(r.is_demo for r in runs)

    segments = status.state_timeline(institution_id, runs, start, now, stale_after)
    current = status.effective_state(
        institution_id, latest if db_state == "ok" else None, now, stale_after
    )

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    chart = {
        "demo": demo,
        "min": ms(start),
        "max": ms(now),
        "segments": [
            {"state": s.state, "label": STATE_UI[s.state][0], "start": ms(s.start), "end": ms(s.end)}
            for s in segments
        ],
    }
    notices, _ = build_notices(cfg, Snapshot(db_state, latest, last_ok), now)
    label, icon, css = STATE_UI[current]
    return {
        "demo": demo,
        "offline_banner": offline_banner(cfg, Snapshot(db_state, latest, last_ok)),
        "notices": notices,
        "info": info,
        "current": {"label": label, "icon": icon, "css": css},
        "period": period,
        "periods": list(PERIODS),
        "chart": chart,
        "segments": [
            {
                "label": STATE_UI[s.state][0],
                "icon": STATE_UI[s.state][1],
                "css": STATE_UI[s.state][2],
                "start": dm_hm(s.start),
                "end": dm_hm(s.end),
            }
            for s in reversed(segments)
        ],
        "transitions": [_transition_view(t, {info.id: info.name}) for t in transitions],
    }
