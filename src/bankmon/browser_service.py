"""Servico da coleta pelo navegador: grava a leitura da aba e mantem o estado (ativo/pausado/...).

Fica FORA de `bankmon.web` de proposito: o dashboard nunca escreve no banco (regra verificada por
teste); a API de coleta delega as gravacoes a este modulo. Nao acessa a rede, cookies nem desafios.

Reserva (SEC-014): a extensao reserva a vaga (`reserve`) ANTES de recarregar a aba; o intervalo
minimo vem das reservas gravadas no banco (nada em memoria) e a reserva e consumida na mesma
transacao da execucao.

Secao critica: a checagem de "pausado", a gravacao da execucao e a do estado acontecem SOB O MESMO
LOCK do runner. Assim uma leitura ok que chegou antes de uma pausa nunca a desfaz (a pausa so sai
pelo clique humano em "Retomar verificacao").
"""

from __future__ import annotations

import logging
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bankmon import db, origins, runner
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.config import Config

log = logging.getLogger("bankmon.browser")

# Motivos gravados em collector_state.motivo (codigos estaveis; a dashboard escolhe o aviso por eles).
BLOCK_REASON = "bloqueio_detectado"
SUSPENDED_REASON = "suspenso_por_desafio"  # desafio em origem EXTERNA: sem retomada pela dashboard
FAILURES_REASON = "falhas_consecutivas"
EXTERNAL_FAILURES_REASON = "falhas_consecutivas_externa"  # idem, em origem EXTERNA: sem retomada
# Pausas de origem externa: so saem por reconfiguracao/nova autorizacao (nunca pelo botao).
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
    """A coleta foi pausada em origem externa (desafio ou falhas): so reconfiguracao/nova autorizacao a reativa."""


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


def read_state(cfg: Config) -> db.CollectorState:
    """Estado sem criar o banco (banco ausente ou sem a tabela = ativo)."""
    if not cfg.db_path.exists():
        return db.CollectorState("ativo", None, None)
    conn = db.open_readonly(cfg.db_path)
    try:
        return db.get_collector_state(conn)
    except sqlite3.OperationalError:
        return db.CollectorState("ativo", None, None)
    finally:
        conn.close()


def request_resume(cfg: Config, now: datetime) -> None:
    """Botao "Retomar verificacao": so autoriza a extensao a checar a pagina de novo.

    Nada e contornado aqui; se a pagina continuar bloqueada, a proxima leitura volta a pausar.
    Sob o lock (levanta `runner.LockBusy`); `ResumeRefused` se a coleta foi suspensa
    em origem externa.
    """
    with runner.file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            current = db.get_collector_state(conn)
            if current.estado != "pausado":
                return
            if current.motivo in NO_RESUME_REASONS:
                raise ResumeRefused("coleta suspensa em origem externa")
            db.set_collector_state(conn, "verificando", RESUME_REASON, now)
        finally:
            conn.close()


def _next_allowed(conn: sqlite3.Connection, cfg: Config, url: str, origin: str) -> datetime | None:
    last = db.last_reservation_at(conn, origin)
    return last + min_interval(cfg, url) if last is not None else None


def check_availability(cfg: Config, url: str, now: datetime) -> Availability:
    """Consulta SOMENTE LEITURA (nunca cria banco nem grava): pode reservar agora?"""
    if not cfg.db_path.exists():
        return Availability(True, None)
    origin = origins.origin_of(url)
    conn = db.open_readonly(cfg.db_path)
    try:
        if db.get_collector_state(conn).estado == "pausado" or db.has_open_reservation(conn, origin, now):
            return Availability(False, None)
        allowed = _next_allowed(conn, cfg, url, origin)
    except sqlite3.OperationalError:  # banco ainda sem as tabelas (versao anterior)
        return Availability(True, None)
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
                if db.get_collector_state(conn).estado == "pausado":
                    raise Paused("coleta pausada")
                if db.has_open_reservation(conn, origin, now):
                    raise ReservationActive("já existe uma reserva aberta para a origem")
                allowed = _next_allowed(conn, cfg, url, origin)
                if allowed is not None and now < allowed:
                    wait = math.ceil((allowed - now).total_seconds())
                    raise TooFrequent("intervalo mínimo entre leituras não cumprido", max(wait, 1))
                reservation = Reservation(secrets.token_urlsafe(24), now + RESERVATION_TTL)
                db.insert_reservation(conn, reservation.reserva_id, origin, now, reservation.expira_em)
        finally:
            conn.close()
    return reservation


def _next_state(
    conn: sqlite3.Connection, status: str, external: bool, previous: db.CollectorState
) -> tuple[str, str | None] | None:
    """Estado seguinte a uma execucao, ou None para manter o atual."""
    if status == "ok":
        return "ativo", None
    if status == "inaccessible":  # pagina de desafio
        return "pausado", SUSPENDED_REASON if external else BLOCK_REASON
    # error: pausa so depois de N leituras seguidas sem sucesso (contadas desde a retomada)
    since = previous.desde if previous.estado == "verificando" else None
    recent = db.recent_statuses(conn, FAILURES_TO_PAUSE, since)
    if len(recent) == FAILURES_TO_PAUSE and all(r != "ok" for r in recent):
        return "pausado", EXTERNAL_FAILURES_REASON if external else FAILURES_REASON
    return None


def ingest(
    cfg: Config,
    url: str,
    html: str,
    now: datetime,
    reserva_id: str | None = None,
    verification: bool = False,
    collector_factory: Callable[[str, str, datetime], BrowserHtmlCollector] = BrowserHtmlCollector,
) -> IngestOutcome:
    """Checa pausa e reserva, roda o coletor e atualiza o estado, tudo sob o lock do runner.

    Leitura normal exige `reserva_id` valido (mesma origem, nao consumido, nao expirado), consumido
    NA MESMA transacao que grava a execucao. `verification=True` (so no estado `verificando` e so em
    origem loopback) le o DOM atual sem reserva. Levanta `runner.LockBusy`, `Paused` (sem gravar
    nada), `TooFrequent` (sem reserva valida) ou `VerificationRefused`.
    """
    clean = origins.strip_query(url)
    origin = origins.origin_of(clean)
    external = not origins.is_loopback_url(clean)
    with runner.file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            previous = db.get_collector_state(conn)
            if previous.estado == "pausado":
                raise Paused("coleta pausada")
            on_record = None
            if verification:
                if previous.estado != "verificando" or external:
                    raise VerificationRefused("verificação indisponível")
            else:
                if not reserva_id or not db.is_reservation_valid(conn, reserva_id, origin, now):
                    raise TooFrequent(NO_RESERVATION_MESSAGE)

                def on_record(c: sqlite3.Connection) -> None:
                    if not db.consume_reservation(c, reserva_id, origin, now):
                        raise TooFrequent(NO_RESERVATION_MESSAGE)

            collector = collector_factory(html, clean, now)
            outcome = runner.collect_and_record(conn, cfg, collector, "scheduled", lambda: now, on_record)
            status = outcome.result.source_status
            change = _next_state(conn, status, external, previous)
            if change is not None:
                db.set_collector_state(conn, change[0], change[1], now)
            state = db.get_collector_state(conn)
        finally:
            conn.close()
    log.info("Coleta do navegador: execução %d, status=%s, estado=%s", outcome.run_id, status, state.estado)
    return IngestOutcome(state.estado, outcome.run_id, collector.page_state, len(outcome.result.services))
