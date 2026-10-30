"""CLI: python -m bankmon <comando>.

Codigos de saida: 0 ok; 1 falha da execucao/comando; 2 lock ocupado (collect)
ou erro de uso (argparse); 3 configuracao invalida.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Sequence

from bankmon import db, extension, runner
from bankmon.collectors.dd_parser import MAX_BYTES
from bankmon.collectors.offline_html import OfflineHtmlCollector
from bankmon.config import NO_ORIGINS_MESSAGE, Config, ConfigError, load_config
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
from bankmon.timeutil import ensure_utc, fmt_local, from_iso, utc_now

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


OFFLINE_COLLECTOR = "offline_html"
CAPTURE_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
OFFLINE_WARNING = "*** DADOS HISTÓRICOS DE VALIDAÇÃO OFFLINE (HTML exportado manualmente), não é monitoramento ao vivo ***"


def offline_config(cfg: Config) -> Config:
    """Mesma configuracao, com o coletor trocado para o offline (banco bankmon-offline.db)."""
    return dataclasses.replace(cfg, collector=OFFLINE_COLLECTOR)


def offline_now(cfg_off: Config) -> datetime:
    """Relogio congelado no `finished_at` da ultima importacao OK (com run_sources), ou o relogio normal."""
    if cfg_off.db_path.exists():
        try:
            conn = db.open_readonly(cfg_off.db_path)
            try:
                row = conn.execute(
                    "SELECT r.finished_at FROM runs r JOIN run_sources s ON s.run_id = r.id "
                    "WHERE r.source_status = 'ok' ORDER BY r.id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return from_iso(row["finished_at"])
        except (sqlite3.Error, ValueError) as exc:
            log.warning("Não foi possível ler a última captura offline: %s", safe_text(str(exc)))
    return utc_now()


BROWSER_COLLECTOR = "navegador"
BROWSER_WARNING = (
    "*** COLETA PELO NAVEGADOR: a extensão envia a leitura da aba aberta pelo usuário "
    "(rode `python -m bankmon extensao` e carregue a pasta gerada) ***"
)


def browser_config(cfg: Config) -> Config:
    """Mesma configuracao, com o coletor trocado para o do navegador (banco bankmon-navegador.db)."""
    return dataclasses.replace(cfg, collector=BROWSER_COLLECTOR)


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    offline = getattr(args, "offline", False)
    if offline:
        cfg = offline_config(cfg)
        if not cfg.db_path.exists():
            print(f"Banco offline inexistente: {cfg.db_path} (rode import-html)", file=sys.stderr)
            return EXIT_FAIL
    now = offline_now(cfg) if offline else utc_now()
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
    if offline:
        print(OFFLINE_WARNING)
    if last_ok is None:
        print("Última verificação válida: nenhuma")
    elif offline:
        print(f"Última verificação válida: {fmt_local(last_ok.finished_at)} (captura histórica)")
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

    from bankmon.web import collect_api, create_app

    clock = utc_now
    api_kwargs: dict = {}
    if getattr(args, "navegador", False):
        if not cfg.browser_origins:
            raise ConfigError(NO_ORIGINS_MESSAGE)
        cfg = browser_config(cfg)
        if cfg.db_path.exists():  # SEC-018: migra (aditivo) antes de servir; leitura nunca ve schema antigo
            try:
                db.open_db(cfg).close()
            except sqlite3.Error as exc:  # SEC-019: ex.: outro `serve` migrou ao mesmo tempo (nada se perde)
                print(f"Erro de banco: {safe_text(str(exc))}", file=sys.stderr)
                return EXIT_FAIL
        api_kwargs = {"coleta_api": True, "coleta_token": extension.load_or_create_token(cfg.data_dir)}
        print(BROWSER_WARNING)
    if getattr(args, "offline", False):
        cfg = offline_config(cfg)
        frozen = offline_now(cfg)
        clock = lambda: frozen  # noqa: E731 - estados como na captura, nao "vencidos"
        print(OFFLINE_WARNING)
    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host  # IPv6 literal na URL
    print(f"Dashboard em http://{host}:{cfg.port}/  (Ctrl+C para encerrar)", flush=True)
    waitress.serve(
        create_app(cfg, clock, **api_kwargs), host=cfg.host, port=cfg.port, threads=4,
        max_request_body_size=collect_api.MAX_BODY_BYTES,
    )
    return EXIT_OK


def cmd_extensao(cfg: Config, args: argparse.Namespace) -> int:
    try:
        folder = extension.build_extension(cfg, args.alvo, args.intervalo)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Erro: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_FAIL
    print(f"Extensão gerada em {folder}")
    print(f"  alvo: {safe_text(args.alvo)}  intervalo: {args.intervalo:g} min  servidor: {extension.server_url(cfg)}")
    print("Para usar: abra a página de extensões do navegador (Edge, Opera GX ou Chrome), ative o")
    print("modo desenvolvedor e use \"Carregar sem pacote\" apontando para a pasta acima; abra a aba do")
    print("alvo e rode `python -m bankmon serve --navegador`. O token fica só em data/ (fora do git).")
    return EXIT_OK


def _file_sha256(path: Path) -> str | None:
    """SHA-256 do arquivo (ate MAX_BYTES); None se ilegivel ou grande demais (o coletor recusa)."""
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > MAX_BYTES else hashlib.sha256(data).hexdigest()


def _captured_at(path: Path, iso: str | None) -> datetime:
    """`--capturado-em` (com fuso) ou a data de modificacao do arquivo, em UTC."""
    try:
        if iso is None:
            moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(iso)
            if moment.tzinfo is None:
                raise ValueError(
                    "--capturado-em precisa de fuso horário (ex.: 2026-10-28T12:00:00-03:00 ou ...Z)"
                )
            moment = ensure_utc(moment)
    except (ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, ValueError) and "fuso" in str(exc):
            raise
        raise ValueError("horário de captura inválido ou fora do intervalo suportado") from None
    if not CAPTURE_MIN <= moment <= utc_now() + timedelta(minutes=2):
        raise ValueError("horário de captura fora do intervalo (2000-01-01 até agora)")
    return moment


def cmd_import_html(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.arquivo)
    if not path.is_file():
        print(f"Arquivo não encontrado: {path.name}", file=sys.stderr)
        return EXIT_FAIL
    try:
        captured = _captured_at(path, args.capturado_em)
    except ValueError as exc:
        print(f"Erro: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_FAIL
    cfg_off = offline_config(cfg)
    sha = _file_sha256(path)
    if sha is not None and cfg_off.db_path.exists():
        conn = db.open_db(cfg_off)
        try:
            if db.source_sha256_exists(conn, sha):
                print("Arquivo já importado (mesmo SHA-256); nada foi gravado.", file=sys.stderr)
                return EXIT_FAIL
        finally:
            conn.close()
    try:
        outcome = runner.run_collection(
            cfg_off, "cli", collector=OfflineHtmlCollector(path, captured)
        )
    except runner.LockBusy as exc:
        log.error("%s", exc)
        return EXIT_LOCKED
    except sqlite3.IntegrityError as exc:
        if "sha256" in str(exc):
            print("Arquivo já importado (mesmo SHA-256); nada foi gravado.", file=sys.stderr)
        else:
            log.error("Violação de integridade na importação: %s", safe_text(str(exc)))
            print("Erro ao gravar a importação (violação de integridade); nada foi gravado.", file=sys.stderr)
        return EXIT_FAIL
    res = outcome.result
    print(
        f"Execução {outcome.run_id}: status={res.source_status} captura={fmt_local(res.finished_at)} "
        f"serviços={len(res.services)} observações={len(res.observations)} "
        f"transições={outcome.transitions}"
    )
    print(f"  banco: {cfg_off.db_path}")
    if res.error_detail:
        print(f"  detalhe: {safe_text(res.error_detail)}")
    return EXIT_OK if res.source_status == "ok" else EXIT_FAIL


COMMANDS = {
    "import-html": cmd_import_html,
    "extensao": cmd_extensao,
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
    serve = sub.add_parser("serve", help="sobe o dashboard em server.host:server.port (somente loopback)")
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="usa o banco de validação offline")
    mode.add_argument("--navegador", action="store_true", help="liga a API da coleta pela aba do navegador")
    ext = sub.add_parser("extensao", help="gera a extensão do navegador em data/extensao-coletor/")
    ext.add_argument("--alvo", required=True, help="URL da aba a ler (origem listada em [coleta_navegador])")
    ext.add_argument("--intervalo", type=float, default=extension.DEFAULT_INTERVAL_MIN, metavar="MIN",
                     help="minutos entre leituras (mínimo 0,5)")
    sub.add_parser("init-db", help="cria o banco e sincroniza o TOML")
    sub.add_parser("check-config", help="valida o TOML")
    collect = sub.add_parser("collect", help="executa uma coleta")
    collect.add_argument("--trigger", choices=runner.TRIGGERS, default="cli")
    status = sub.add_parser("status", help="mostra o estado atual no terminal")
    status.add_argument("--offline", action="store_true", help="usa o banco de validação offline")
    imp = sub.add_parser("import-html", help="importa um HTML do Downdetector exportado manualmente")
    imp.add_argument("arquivo")
    imp.add_argument("--capturado-em", metavar="ISO", help="horário da captura, com fuso (padrão: data do arquivo)")
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
    except ConfigError as exc:
        print(f"Erro de configuração: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_CONFIG
    except db.DatabaseError as exc:
        print(f"Erro de banco: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        close_logging()
