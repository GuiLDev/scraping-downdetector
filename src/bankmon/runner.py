"""Execucao de uma coleta: lockfile, coletor ativo e gravacao atomica."""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from bankmon import db
from bankmon.collectors.base import (
    LEVELS,
    SOURCE_STATUSES,
    CollectionResult,
    Clock,
    Collector,
    Observation,
)
from bankmon.collectors.registry import create_collector
from bankmon.config import Config
from bankmon.status import RunView, effective_state
from bankmon.textutil import safe_error, safe_text
from bankmon.timeutil import utc_now

log = logging.getLogger("bankmon.runner")

TRIGGERS = ("scheduled", "cli")


class LockBusy(Exception):
    """Outro processo mantem o lock do SO: ja existe uma coleta em andamento."""


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    result: CollectionResult
    transitions: int


def _try_os_lock(fd: int) -> bool:
    """Lock exclusivo e nao bloqueante do SO sobre o primeiro byte do arquivo."""
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Lock do SO mantido aberto durante a execucao.

    O SO libera o lock se o processo morrer, entao nao ha lock "velho" a limpar.
    O arquivo nunca e apagado (evita corrida entre remocao e criacao).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        locked = _try_os_lock(fd)
        if not locked:
            raise LockBusy(f"coleta já em andamento (lock: {lock_path})")
        yield
    finally:
        if locked and sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                log.warning("Falha ao destravar o lock explicitamente: %s", lock_path)
        os.close(fd)  # fechar o descritor libera o lock em qualquer SO


MAX_FUTURE_SKEW = timedelta(minutes=2)


def _is_aware(dt: object) -> bool:
    return isinstance(dt, datetime) and dt.tzinfo is not None and dt.utcoffset() is not None


def _validate(
    result: CollectionResult, cfg: Config, valid_ids: set[str], now: datetime, expected: str
) -> str | None:
    """Descreve o primeiro problema do resultado, ou None se for valido."""
    if not isinstance(result, CollectionResult):
        return f"coletor não devolveu CollectionResult ({type(result).__name__})"
    if not isinstance(result.collector, str) or result.collector != expected:
        return "collector diferente do coletor ativo"
    if not isinstance(result.is_demo, bool):
        return "is_demo não é bool"
    if not isinstance(result.started_at, datetime) or not isinstance(result.finished_at, datetime):
        return "horários não são datetime"
    if not (_is_aware(result.started_at) and _is_aware(result.finished_at)):
        return "horários sem fuso horário"
    if result.started_at > result.finished_at:
        return "started_at posterior a finished_at"
    if result.finished_at > now + MAX_FUTURE_SKEW:
        return "finished_at no futuro"
    if result.is_demo != cfg.is_demo:
        return f"is_demo={result.is_demo} incompatível com o modo do banco"
    if not isinstance(result.source_status, str) or result.source_status not in SOURCE_STATUSES:
        return "source_status inválido"
    if result.error_detail is not None and not isinstance(result.error_detail, str):
        return "error_detail não é str"
    if not isinstance(result.observations, (tuple, list)):
        return "observations não é sequência"
    seen: set[str] = set()
    for obs in result.observations:
        if not isinstance(obs, Observation):
            return "observação não é Observation"
        if not isinstance(obs.institution_id, str) or not isinstance(obs.level, str):
            return "institution_id/level não são str"
        if obs.detail is not None and not isinstance(obs.detail, str):
            return f"detail não é str ({obs.institution_id})"
        if obs.level not in LEVELS:
            return f"nível inválido para {obs.institution_id}"
        if obs.institution_id not in valid_ids:
            return "instituição desconhecida/desabilitada"
        if obs.institution_id in seen:
            return "observação duplicada"
        seen.add(obs.institution_id)
    return None


def _sanitize(
    result: CollectionResult,
    cfg: Config,
    valid_ids: set[str],
    now: datetime,
    expected: str | None = None,
) -> CollectionResult:
    """Resultado invalido do coletor vira execucao `error` em vez de corromper o banco.

    Qualquer excecao durante a validacao tambem vira `error`: a execucao e sempre gravada.
    """
    expected = expected or cfg.collector
    try:
        problem = _validate(result, cfg, valid_ids, now, expected)
    except Exception as exc:  # noqa: BLE001 - objeto malformado do coletor
        problem = f"falha ao validar o resultado ({type(exc).__name__})"
    if problem is None:
        return dataclasses.replace(
            result,
            error_detail=safe_text(result.error_detail) if result.error_detail else None,
            observations=tuple(
                dataclasses.replace(o, detail=safe_text(o.detail) if o.detail else None)
                for o in result.observations
            ),
        )
    log.error("Resultado inválido do coletor: %s", safe_text(problem))
    # Resultado novo (sem replace): `result` pode nem ser um CollectionResult.
    return CollectionResult(
        collector=expected,
        is_demo=cfg.is_demo,
        started_at=now,
        finished_at=now,
        source_status="error",
        observations=(),
        error_detail=safe_text(f"Resultado inválido do coletor: {problem}"),
    )


def record_result(
    conn: sqlite3.Connection,
    cfg: Config,
    result: CollectionResult,
    trigger: str,
    clock: Clock | None = None,
    expected_collector: str | None = None,
) -> RunOutcome:
    """Grava run + observations + state_transitions numa unica transacao.

    A transicao compara o estado efetivo imediatamente antes (com expiracao
    aplicada em `finished_at`) com o estado depois, por instituicao habilitada.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger inválido: {trigger!r}")
    clock = clock or utc_now  # resolvido na chamada (permite simular o relogio)
    stale_after = timedelta(minutes=cfg.stale_after_minutes)
    with db.transaction(conn):
        infos = db.list_institution_infos(conn)
        result = _sanitize(result, cfg, {i.id for i in infos}, clock(), expected_collector)
        at = result.finished_at
        previous = db.latest_run(conn)
        run_id = db.insert_run(conn, result, trigger)
        db.insert_observations(conn, run_id, result.observations)
        current = RunView(
            run_id=run_id,
            finished_at=at,
            source_status=result.source_status,
            is_demo=result.is_demo,
            levels={o.institution_id: o.level for o in result.observations},
            error_detail=result.error_detail,
        )
        transitions = []
        for info in infos:
            before = effective_state(info.id, previous, at, stale_after)
            after = effective_state(info.id, current, at, stale_after)
            if before != after:
                transitions.append((info.id, before, after))
        db.insert_transitions(conn, run_id, transitions, at)
    return RunOutcome(run_id, result, len(transitions))


def _collect_safely(
    collector: Collector, conn: sqlite3.Connection, cfg: Config, clock
) -> CollectionResult:
    started = clock()
    try:
        return collector.collect(db.list_institution_refs(conn))
    except Exception as exc:  # noqa: BLE001 - qualquer falha do coletor vira execucao `error`
        log.error("Coletor %s falhou: %s", collector.name, safe_error(exc))
        return CollectionResult(
            collector=collector.name,
            is_demo=collector.is_demo,
            started_at=started,
            finished_at=clock(),
            source_status="error",
            observations=(),
            error_detail=safe_error(exc),
        )


def run_collection(
    cfg: Config,
    trigger: str,
    now: datetime | None = None,
    collector: Collector | None = None,
) -> RunOutcome:
    """Executa o coletor ativo uma vez. Levanta `LockBusy` se ja houver coleta em curso.

    `now` fixa o relogio (testes/simulacao); `collector` permite injetar um coletor.
    """
    clock = (lambda: now) if now is not None else utc_now
    with file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            if collector is None:
                collector = create_collector(cfg.collector, seed=cfg.demo_seed, clock=clock)
            result = _collect_safely(collector, conn, cfg, clock)
            outcome = record_result(conn, cfg, result, trigger, clock, collector.name)
        finally:
            conn.close()
    log.info(
        "Execução %d: coletor=%s status=%s observações=%d transições=%d",
        outcome.run_id,
        outcome.result.collector,
        outcome.result.source_status,
        len(outcome.result.observations),
        outcome.transitions,
    )
    return outcome


def seed_demo(cfg: Config, days: int, now: datetime | None = None) -> int:
    """Gera execucoes horarias retroativas (ordem cronologica). Retorna quantas gravou."""
    if not cfg.is_demo:
        raise ValueError("seed-demo só é permitido com o coletor ativo 'demo'")
    if days <= 0:
        raise ValueError("--days deve ser maior que zero")
    end = (now or utc_now()).replace(microsecond=0)
    first = end - timedelta(hours=days * 24)
    with file_lock(cfg.lock_path):
        conn = db.open_db(cfg)
        try:
            if db.has_runs_since(conn, first):
                raise ValueError(
                    "o banco já tem execuções no período; remova o arquivo "
                    f"{cfg.db_path} para regenerar o histórico"
                )
            count = 0
            for k in range(days * 24, 0, -1):
                moment = end - timedelta(hours=k)
                collector = create_collector(
                    cfg.collector, seed=cfg.demo_seed, clock=lambda m=moment: m
                )
                result = _collect_safely(collector, conn, cfg, lambda m=moment: m)
                record_result(conn, cfg, result, "cli", lambda m=moment: m)
                count += 1
        finally:
            conn.close()
    log.info("seed-demo: %d execuções geradas", count)
    return count
