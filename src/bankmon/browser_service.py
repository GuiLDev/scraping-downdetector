"""Servico da coleta pelo navegador: grava a leitura da aba e mantem o estado (ativo/pausado/...).

Fica FORA de `bankmon.web` de proposito: o dashboard nunca escreve no banco (regra verificada por
teste); a API de coleta delega as gravacoes a este modulo. Nao acessa a rede, cookies nem desafios.

Reserva (SEC-014): a extensao reserva a vaga (`reserve`) ANTES de recarregar a aba; o intervalo
minimo vem das reservas gravadas no banco (nada em memoria) e a reserva e consumida na mesma
transacao da execucao.

Estado POR ORIGEM (schema v6, `collector_origin_state`): a pausa de uma origem nao afeta as outras.
A linha global antiga de `collector_state` esta congelada e so vale como fallback de leitura para a
origem sem linha propria. O contador `falhas_seguidas` da origem e gravado NA MESMA TRANSACAO da
execucao (`on_record` do runner), junto com o consumo da reserva: nao ha janela entre gravar a
execucao e gravar o estado.

Secao critica: a checagem de "pausado", a gravacao da execucao e a do estado acontecem SOB O MESMO
LOCK do runner. Assim uma leitura ok que chegou antes de uma pausa nunca a desfaz (a pausa so sai
pelo clique humano em "Retomar verificacao", e so em origem loopback).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bankmon import db, origins, runner
from bankmon.collectors.base import CollectionResult
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.config import Config

log = logging.getLogger("bankmon.browser")

# Motivos gravados em collector_origin_state.motivo (codigos estaveis; a dashboard escolhe o aviso por eles).
BLOCK_REASON = "bloqueio_detectado"
SUSPENDED_REASON = "suspenso_por_desafio"  # desafio em origem EXTERNA: sem retomada pela dashboard
FAILURES_REASON = "falhas_consecutivas"
EXTERNAL_FAILURES_REASON = "falhas_consecutivas_externa"  # idem, em origem EXTERNA: sem retomada
# Pausas de origem externa: nunca saem pelo botao nem trocando a origem; so com um banco novo,
# depois da revisao da autorizacao (SEC-024).
NO_RESUME_REASONS = (SUSPENDED_REASON, EXTERNAL_FAILURES_REASON)
RESUME_REASON = "retomada pelo usuário; aguardando a próxima leitura da aba"

FAILURES_TO_PAUSE = 3
EXTERNAL_MIN_INTERVAL = timedelta(minutes=60)  # origem externa: fixo, nao configuravel
RESERVATION_TTL = timedelta(minutes=2)
VERIFICATION_MODE = "verificacao"
# Mensagem unica: nao revela se a reserva nao existe, expirou, foi usada ou e de outra origem.
NO_RESERVATION_MESSAGE = "reserva ausente, inválida ou expirada"


class Paused(Exception):
    """Coleta pausada: reserva e leitura sao recusadas (409) sem gravar nada."""


class ReservationActive(Exception):
    """Ja existe uma reserva aberta (nao consumida e nao expirada) para a origem (409)."""


class TooFrequent(Exception):
    """Intervalo minimo desde a ultima recarga nao cumprido, ou leitura sem reserva valida (429)."""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class VerificationRefused(Exception):
    """Leitura em modo verificacao fora do estado `verificando` ou em origem externa (409)."""


class ResumeRefused(Exception):
    """A coleta foi pausada em origem externa (desafio ou falhas): so um banco novo a reativa."""


@dataclass(frozen=True)
class Reservation:
    reserva_id: str
    expira_em: datetime


@dataclass(frozen=True)
class IngestOutcome:
    estado: str
    run_id: int
    page_state: str
    servicos: int


@dataclass(frozen=True)
class Availability:
    """Resposta de consulta: pode reservar agora? Se nao por intervalo, quando."""

    pode_reservar: bool
    proxima_leitura_em: datetime | None


def min_interval(cfg: Config, url: str) -> timedelta:
    """Externa: 60 min fixos. Loopback: `[coleta_navegador] intervalo_minimo_local_s`."""
    if origins.is_loopback_url(url):
        return timedelta(seconds=cfg.browser_local_interval_s)
    return EXTERNAL_MIN_INTERVAL


def effective_collector_state(origin: str, state: db.CollectorState) -> db.CollectorState:
    """INF-01: origem externa nao tem retomada. Pausa ou verificacao que sobrou de uma configuracao
    loopback anterior (linha legada) vale como suspensao de origem externa. Decide pela origem."""
    if origins.is_loopback_url(origin) or state.estado == "ativo":
        return state
    if state.estado == "pausado" and state.motivo in NO_RESUME_REASONS:
        return state
    motivo = EXTERNAL_FAILURES_REASON if state.motivo == FAILURES_REASON else SUSPENDED_REASON
    return dataclasses.replace(state, estado="pausado", motivo=motivo)


def configured_origins(cfg: Config) -> tuple[str, ...]:
    """Origens da lista autorizada em forma canonica (esquema://host[:porta]), sem repeticao."""
    return tuple(dict.fromkeys(origins.origin_of(p) for p in cfg.browser_origins))


def can_resume(origin: str, state: db.CollectorState) -> bool:
    """O botao "Retomar verificacao" existe so para origem loopback pausada por motivo retomavel."""
    return (
        state.estado == "pausado"
        and state.motivo not in NO_RESUME_REASONS
        and origins.is_loopback_url(origin)
    )


def origin_state(origin: str, conn: sqlite3.Connection) -> db.CollectorState:
    """Estado efetivo da origem (unico ponto de leitura: servico e dashboard).

    SEC-024: a suspensao de origem EXTERNA (desafio ou falhas) vale para QUALQUER outra origem
    externa do mesmo banco; trocar o nome da origem externa na config nao a reativa. So
    origens externas recebem esses motivos, entao as linhas v6 nunca afetam a origem loopback
    (a excecao e a linha legada anterior a v6: suspensa, ela vale para a loopback sem linha propria).
    """
    state = effective_collector_state(origin, db.get_origin_state(conn, origin))
    if origins.is_loopback_url(origin) or (state.estado == "pausado" and state.motivo in NO_RESUME_REASONS):
        return state
    suspension = db.external_suspension(conn, NO_RESUME_REASONS)
    if suspension is None:
        return state
    return db.CollectorState("pausado", suspension[0], suspension[1], state.falhas_seguidas)


def read_state(cfg: Config, url: str) -> db.CollectorState:
    """Estado da origem de `url` sem criar o banco (banco ausente = ativo). Banco ilegivel ou com
    schema antigo levanta `sqlite3.Error` (SEC-018: nunca vira "ativo"; `serve --navegador` migra
    antes de servir)."""
    if not cfg.db_path.exists():
        return db.CollectorState("ativo", None, None)
    origin = origins.origin_of(url)
    conn = db.open_readonly(cfg.db_path)
    try:
        return origin_state(origin, conn)
    finally:
        conn.close()


def request_resume(cfg: Config, origin: str, now: datetime) -> None:
    """Botao "Retomar verificacao" da origem: so autoriza a extensao a checar a pagina de novo.

    Nada e contornado aqui; se a pagina continuar bloqueada, a proxima leitura volta a pausar.
    `ResumeRefused` (sem tocar no banco) se a origem nao for loopback; sob o lock (levanta
    `runner.LockBusy`), tambem se a pausa for de origem externa herdada (`NO_RESUME_REASONS`).
    """
    if not origins.is_loopback_url(origin):
        raise ResumeRefused("coleta suspensa em origem externa")
    with runner.file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            current = origin_state(origin, conn)
            if current.estado != "pausado":
                return
            if current.motivo in NO_RESUME_REASONS:
                raise ResumeRefused("coleta suspensa em origem externa")
            db.set_origin_state(conn, origin, "verificando", RESUME_REASON, now)
        finally:
            conn.close()


def _next_allowed(conn: sqlite3.Connection, cfg: Config, url: str, origin: str) -> datetime | None:
    last = db.last_reservation_at(conn, origin)
    return last + min_interval(cfg, url) if last is not None else None


def check_availability(cfg: Config, url: str, now: datetime) -> Availability:
    """Consulta SOMENTE LEITURA (nunca cria banco nem grava): pode reservar agora? Banco ilegivel ou
    com schema antigo levanta `sqlite3.Error` (SEC-018: nunca vira "pode reservar")."""
    if not cfg.db_path.exists():
        return Availability(True, None)
    origin = origins.origin_of(url)
    conn = db.open_readonly(cfg.db_path)
    try:
        if origin_state(origin, conn).estado == "pausado" or db.has_open_reservation(conn, origin, now):
            return Availability(False, None)
        allowed = _next_allowed(conn, cfg, url, origin)
    finally:
        conn.close()
    if allowed is not None and now < allowed:
        return Availability(False, allowed)
    return Availability(True, None)


def reserve(cfg: Config, url: str, now: datetime) -> Reservation:
    """Reserva a vaga de leitura ANTES de qualquer recarga (BEGIN IMMEDIATE, sob o lock do runner).

    Recusas, nesta ordem: `Paused`, `ReservationActive`, `TooFrequent` (com `retry_after` em s).
    Levanta `runner.LockBusy` se outra coleta/reserva estiver em curso.
    """
    origin = origins.origin_of(url)
    with runner.file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            with db.transaction(conn):
                if origin_state(origin, conn).estado == "pausado":
                    raise Paused("coleta pausada")
                if db.has_open_reservation(conn, origin, now):
                    raise ReservationActive("já existe uma reserva aberta para a origem")
                allowed = _next_allowed(conn, cfg, url, origin)
                if allowed is not None and now < allowed:
                    wait = math.ceil((allowed - now).total_seconds())
                    raise TooFrequent("intervalo mínimo entre leituras não cumprido", max(wait, 1))
                reservation = Reservation(secrets.token_urlsafe(24), now + RESERVATION_TTL)
                db.insert_reservation(conn, reservation.reserva_id, origin, now, reservation.expira_em)
                # INF-02: o intervalo conta da reserva mais recente; as fechadas nao servem mais
                db.prune_reservations(conn, origin, reservation.reserva_id, now)
        finally:
            conn.close()
    return reservation


def _next_state(
    status: str, external: bool, previous: db.CollectorState
) -> tuple[str, str | None, int]:
    """Funcao pura: `(estado, motivo, falhas_seguidas)` seguintes a uma execucao da origem.

    Pausa por falhas ao chegar a `FAILURES_TO_PAUSE` leituras seguidas sem sucesso (contador por
    origem: exato no mesmo segundo e depois de reinicio; zera num ok e na retomada).
    """
    if status == "ok":
        return "ativo", None, 0
    failures = previous.falhas_seguidas + 1
    if status == "inaccessible":  # pagina de desafio
        return "pausado", SUSPENDED_REASON if external else BLOCK_REASON, failures
    if failures >= FAILURES_TO_PAUSE:
        return "pausado", EXTERNAL_FAILURES_REASON if external else FAILURES_REASON, failures
    return previous.estado, previous.motivo, failures


def ingest(
    cfg: Config,
    url: str,
    html: str,
    now: datetime,
    reserva_id: str | None = None,
    verification: bool = False,
    collector_factory: Callable[[str, str, datetime], BrowserHtmlCollector] = BrowserHtmlCollector,
) -> IngestOutcome:
    """Checa pausa e reserva, roda o coletor e atualiza o estado da origem, tudo sob o lock do runner.

    Leitura normal exige `reserva_id` valido (mesma origem, nao consumido, nao expirado), consumido
    NA MESMA transacao que grava a execucao e o estado da origem. `verification=True` (so no estado `verificando` e so em
    origem loopback) le o DOM atual sem reserva. Levanta `runner.LockBusy`, `Paused` (sem gravar
    nada), `TooFrequent` (sem reserva valida) ou `VerificationRefused`.
    """
    clean = origins.strip_query(url)
    origin = origins.origin_of(clean)
    external = not origins.is_loopback_url(clean)
    with runner.file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            previous = origin_state(origin, conn)
            if previous.estado == "pausado":
                raise Paused("coleta pausada")
            if verification:
                if previous.estado != "verificando" or external:
                    raise VerificationRefused("verificação indisponível")
            elif not reserva_id or not db.is_reservation_valid(conn, reserva_id, origin, now):
                raise TooFrequent(NO_RESERVATION_MESSAGE)

            def on_record(c: sqlite3.Connection, result: CollectionResult) -> None:
                # na transacao da execucao: reserva consumida e estado/contador da origem juntos
                if not verification and not db.consume_reservation(c, reserva_id, origin, now):
                    raise TooFrequent(NO_RESERVATION_MESSAGE)
                db.write_origin_state(c, origin, *_next_state(result.source_status, external, previous), now)

            collector = collector_factory(html, clean, now)
            outcome = runner.collect_and_record(conn, cfg, collector, "scheduled", lambda: now, on_record)
            status = outcome.result.source_status
            state = origin_state(origin, conn)
        finally:
            conn.close()
    log.info("Coleta do navegador: execução %d, status=%s, estado=%s", outcome.run_id, status, state.estado)
    return IngestOutcome(state.estado, outcome.run_id, collector.page_state, len(outcome.result.services))
