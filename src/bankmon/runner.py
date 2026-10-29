"""Execucao de uma coleta: lockfile, coletor ativo e gravacao atomica."""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from bankmon import db
from bankmon.collectors.base import (
    LEVELS,
    SERVICE_COLORS,
    SERVICE_STATUSES,
    SOURCE_KINDS,
    SOURCE_STATUSES,
    CollectionResult,
    Clock,
    Collector,
    Observation,
    ServiceRecord,
    SourceInfo,
)
from bankmon.collectors import svg_geometry
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
MAX_SERVICES = 1000
SERVICE_TEXT_LEN = 200
_SHA256 = re.compile(r"[0-9a-f]{64}")
# URL da aba lida: http(s), sem espacos/controle, sem userinfo, query string nem fragmento.
_SOURCE_URL = re.compile(r"https?://[^\s?#@\x00-\x1f]{1,480}")
# campo do ServiceRecord -> validador da geometria (valor valido deve voltar identico)
_GEOMETRY_VALIDATORS = {
    "svg_viewbox": svg_geometry.validate_viewbox,
    "svg_width": svg_geometry.validate_dimension,
    "svg_height": svg_geometry.validate_dimension,
    "svg_path_d": svg_geometry.validate_path_d,
}


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
    return _validate_services(result, valid_ids) or _validate_source(result.source)


def _validate_services(result: CollectionResult, valid_ids: set[str]) -> str | None:
    services = result.services
    if not isinstance(services, (tuple, list)):
        return "services não é sequência"
    if len(services) > MAX_SERVICES:
        return f"mais de {MAX_SERVICES} serviços"
    positions: set[int] = set()
    for svc in services:
        if not isinstance(svc, ServiceRecord):
            return "serviço não é ServiceRecord"
        if not isinstance(svc.position, int) or isinstance(svc.position, bool) or svc.position < 0:
            return "position do serviço inválida"
        if svc.position in positions:
            return "position de serviço duplicada"
        positions.add(svc.position)
        if not isinstance(svc.service, str):
            return "nome do serviço não é str"
        for name in ("status_original", "issue"):
            value = getattr(svc, name)
            if value is not None and not isinstance(value, str):
                return f"{name} do serviço não é str"
        if svc.status not in SERVICE_STATUSES:
            return "status de serviço inválido"
        if svc.color is not None and svc.color not in SERVICE_COLORS:
            return "color de serviço inválida"
        if not isinstance(svc.consistent, bool):
            return "consistent do serviço não é bool"
        if svc.institution_id is not None and svc.institution_id not in valid_ids:
            return "instituição do serviço desconhecida/desabilitada"
        for name, validate in _GEOMETRY_VALIDATORS.items():
            value = getattr(svc, name)
            if value is not None and validate(value) != value:
                return f"geometria inválida em {name}"
    return None


def _validate_source(source: object) -> str | None:
    if source is None:
        return None
    if not isinstance(source, SourceInfo):
        return "source não é SourceInfo"
    if not isinstance(source.file_name, str) or not source.file_name:
        return "file_name inválido"
    if any(c in source.file_name for c in "/\\:") or Path(source.file_name).name != source.file_name:
        return "file_name com caminho"
    if not isinstance(source.sha256, str) or not _SHA256.fullmatch(source.sha256):
        return "sha256 inválido"
    if not isinstance(source.size_bytes, int) or isinstance(source.size_bytes, bool) or source.size_bytes < 0:
        return "size_bytes inválido"
    if not _is_aware(source.captured_at):
        return "captured_at sem fuso horário"
    if source.kind not in SOURCE_KINDS:
        return "kind da origem inválido"
    if source.kind == "browser":
        if not isinstance(source.url, str) or not _SOURCE_URL.fullmatch(source.url):
            return "url da origem inválida (esperada http(s) sem query string)"
    elif source.url is not None:
        return "url só é permitida na origem do navegador"
    return None


def _safe_opt(text: str | None) -> str | None:
    return safe_text(text, SERVICE_TEXT_LEN) if text else None


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
            services=tuple(
                dataclasses.replace(
                    v,
                    service=safe_text(v.service, SERVICE_TEXT_LEN),
                    status_original=_safe_opt(v.status_original),
                    issue=_safe_opt(v.issue),
                )
                for v in result.services
            ),
            source=(
                dataclasses.replace(result.source, file_name=safe_text(result.source.file_name, SERVICE_TEXT_LEN))
                if result.source is not None
                else None
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
    on_record: Callable[[sqlite3.Connection], None] | None = None,
) -> RunOutcome:
    """Grava run + observations + state_transitions numa unica transacao.

    `on_record(conn)` roda DENTRO da transacao, logo apos inserir a execucao: se ele ou qualquer
    gravacao seguinte falhar, tudo (inclusive o que `on_record` fez) volta atras.

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
        if on_record is not None:
            on_record(conn)
        db.insert_observations(conn, run_id, result.observations)
        if result.source is not None:
            db.insert_run_source(conn, run_id, result.source, clock())
        if result.services:
            db.insert_run_services(conn, run_id, result.services)
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


def collect_and_record(
    conn: sqlite3.Connection,
    cfg: Config,
    collector: Collector,
    trigger: str,
    clock,
    on_record: Callable[[sqlite3.Connection], None] | None = None,
) -> RunOutcome:
    """Coleta e grava (numa transacao) com a conexao dada. O CHAMADOR deve segurar `file_lock`:
    serve a quem precisa de outras leituras/gravacoes na mesma secao critica (ex.: estado da coleta)."""
    result = _collect_safely(collector, conn, cfg, clock)
    return record_result(conn, cfg, result, trigger, clock, collector.name, on_record=on_record)


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
            outcome = collect_and_record(conn, cfg, collector, trigger, clock)
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
