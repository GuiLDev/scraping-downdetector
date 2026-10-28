"""CLI: python -m bankmon <comando>.

Codigos de saida: 0 ok; 1 falha da execucao/comando; 2 lock ocupado (collect)
ou erro de uso (argparse); 3 configuracao invalida.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from typing import Sequence

from bankmon import db, runner
from bankmon.config import Config, ConfigError, load_config
from bankmon.status import (
    ATTENTION,
    CRITICAL,
    NO_ALERT,
    NO_DATA,
    NOT_MONITORED,
    build_cards,
    group_and_sort,
)
from bankmon.textutil import safe_text
from bankmon.timeutil import fmt_local, utc_now

EXIT_OK, EXIT_FAIL, EXIT_LOCKED, EXIT_CONFIG = 0, 1, 2, 3

STATE_LABELS = {
    CRITICAL: "Crítico",
    ATTENTION: "Atenção",
    NO_ALERT: "Sem alerta",
    NO_DATA: "Sem dados",
    NOT_MONITORED: "Não monitorado",
}
DEMO_WARNING = "*** DADOS DE DEMONSTRAÇÃO, não refletem o Downdetector ***"
DISCLAIMER = "Indicador do Downdetector, não é confirmação oficial."

log = logging.getLogger("bankmon")


def setup_logging(cfg: Config) -> None:
    """Log rotativo em data/logs/bankmon.log e em stderr (reconfigura sem duplicar).

    Sem console (pythonw.exe, Agendador de Tarefas) sys.stderr e None: so o arquivo.
    """
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            cfg.log_dir / "bankmon.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger("bankmon")
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def close_logging() -> None:
    """Fecha e remove os handlers do logger `bankmon` (nao deixa estado global)."""
    root = logging.getLogger("bankmon")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = True


def cmd_init_db(cfg: Config, args: argparse.Namespace) -> int:
    db.open_db(cfg).close()
    print(f"Banco pronto: {cfg.db_path}")
    return EXIT_OK


def cmd_check_config(cfg: Config, args: argparse.Namespace) -> int:
    enabled = sum(1 for i in cfg.institutions if i.enabled)
    print(f"Configuração OK: {cfg.config_path}")
    print(f"  coletor ativo : {cfg.collector} ({'demonstração' if cfg.is_demo else 'real'})")
    print(f"  categorias    : {len(cfg.categories)}")
    print(f"  instituições  : {len(cfg.institutions)} ({enabled} habilitadas)")
    print(f"  intervalo     : {cfg.interval_minutes} min (dado vale por {cfg.stale_after_minutes} min)")
    print(f"  servidor      : {cfg.host}:{cfg.port}")
    print(f"  banco         : {cfg.db_path}")
    return EXIT_OK


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    try:
        outcome = runner.run_collection(cfg, args.trigger)
    except runner.LockBusy as exc:
        log.error("%s", exc)
        return EXIT_LOCKED
    res = outcome.result
    print(
        f"Execução {outcome.run_id}: status={res.source_status} "
        f"observações={len(res.observations)} transições={outcome.transitions}"
    )
    if res.error_detail:
        print(f"  detalhe: {safe_text(res.error_detail)}")
    return EXIT_OK if res.source_status == "ok" else EXIT_FAIL


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    now = utc_now()
    stale_after = timedelta(minutes=cfg.stale_after_minutes)
    conn = db.open_db(cfg)
    try:
        infos = db.list_institution_infos(conn)
        latest = db.latest_run(conn)
        last_ok = db.latest_ok_run(conn)
    finally:
        conn.close()

    if cfg.is_demo or (latest is not None and latest.is_demo):
        print(DEMO_WARNING)
    if last_ok is None:
        print("Última verificação válida: nenhuma")
    else:
        minutes = int((now - last_ok.finished_at).total_seconds() // 60)
        print(f"Última verificação válida: {fmt_local(last_ok.finished_at)} (há {minutes} min)")
    if latest is not None and latest.source_status != "ok":
        print(
            f"Fonte: {latest.source_status} em {fmt_local(latest.finished_at)}"
            + (f" - {safe_text(latest.error_detail)}" if latest.error_detail else "")
        )
    print(DISCLAIMER)

    cards = build_cards(infos, latest, now, stale_after)
    for section in group_and_sort(cards):
        print(f"\n== {section.title} ({len(section.cards)}) ==")
        for card in section.cards:
            print(f"  {STATE_LABELS[card.state]:<15} {card.name:<20} {card.category_name}")
    return EXIT_OK


def cmd_seed_demo(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.is_demo:
        log.error("seed-demo só é permitido com o coletor ativo 'demo' (atual: %s)", cfg.collector)
        return EXIT_FAIL
    try:
        count = runner.seed_demo(cfg, args.days)
    except runner.LockBusy as exc:
        log.error("%s", exc)
        return EXIT_LOCKED
    except ValueError as exc:
        log.error("%s", safe_text(str(exc)))
        return EXIT_FAIL
    print(f"{count} execuções sintéticas geradas em {cfg.db_path}")
    return EXIT_OK


def cmd_backup(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.db_path.exists():
        log.error("banco inexistente: %s (rode init-db)", cfg.db_path)
        return EXIT_FAIL
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    target = cfg.backup_dir / f"{cfg.db_path.stem}-{stamp}.db"
    n = 1
    while target.exists():  # nunca sobrescreve um backup existente
        target = cfg.backup_dir / f"{cfg.db_path.stem}-{stamp}-{n}.db"
        n += 1
    src = sqlite3.connect(cfg.db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"Backup gravado em {target}")
    return EXIT_OK


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    """Sobe o dashboard (waitress) em cfg.host:cfg.port (loopback validado em load_config)."""
    import waitress

    from bankmon.web import create_app

    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host  # IPv6 literal na URL
    print(f"Dashboard em http://{host}:{cfg.port}/  (Ctrl+C para encerrar)", flush=True)
    waitress.serve(create_app(cfg), host=cfg.host, port=cfg.port, threads=4)
    return EXIT_OK


COMMANDS = {
    "serve": cmd_serve,
    "init-db": cmd_init_db,
    "check-config": cmd_check_config,
    "collect": cmd_collect,
    "status": cmd_status,
    "seed-demo": cmd_seed_demo,
    "backup": cmd_backup,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bankmon", description="Downdetector Banking Monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="sobe o dashboard em server.host:server.port (somente loopback)")
    sub.add_parser("init-db", help="cria o banco e sincroniza o TOML")
    sub.add_parser("check-config", help="valida o TOML")
    collect = sub.add_parser("collect", help="executa uma coleta")
    collect.add_argument("--trigger", choices=runner.TRIGGERS, default="cli")
    sub.add_parser("status", help="mostra o estado atual no terminal")
    seed = sub.add_parser("seed-demo", help="gera histórico sintético (só no modo demo)")
    seed.add_argument("--days", type=int, default=7)
    sub.add_parser("backup", help="copia o banco para data/backups/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Erro de configuração: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_CONFIG
    if args.command != "check-config":
        setup_logging(cfg)
    try:
        return COMMANDS[args.command](cfg, args)
    except db.DatabaseError as exc:
        print(f"Erro de banco: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        close_logging()
