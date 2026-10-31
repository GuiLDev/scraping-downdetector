"""Gera a extensao do navegador (modelo em `extension/coletor/`) pronta para carregar.

O token da coleta e o `config.json` ficam em `data/` (ignorado pelo git); o modelo versionado nunca
tem segredos. A extensao so le a aba que o usuario ja abriu: sem cookies, sem tratar desafios.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from bankmon import browser_service, origins
from bankmon.config import NO_ORIGINS_MESSAGE, PROJECT_ROOT, Config

TOKEN_FILE = "coleta-token.txt"
OUTPUT_DIR = "extensao-coletor"
TEMPLATE_DIR = PROJECT_ROOT / "extension" / "coletor"
DEFAULT_INTERVAL_MIN = 5.0
MIN_INTERVAL_MIN = 0.5  # minimo dos alarmes das extensoes (30 s)
EXTERNAL_MIN_INTERVAL_MIN = browser_service.EXTERNAL_MIN_INTERVAL.total_seconds() / 60  # 1 leitura/hora
TOKEN_BYTES = 32


def load_or_create_token(data_dir: Path) -> str:
    """Token da coleta: reaproveita `data/coleta-token.txt` ou cria um novo (acesso so do dono)."""
    path = data_dir / TOKEN_FILE
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= TOKEN_BYTES:
            return token
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # no Windows so afeta o atributo somente leitura; melhor esforco
    except OSError:
        pass
    return token


def validate_target(target: str, patterns: tuple[str, ...]) -> str:
    """Alvo precisa estar nas origens autorizadas e nao ter query string nem fragmento."""
    if not patterns:
        raise ValueError(NO_ORIGINS_MESSAGE)
    parts = urlsplit(target) if isinstance(target, str) else None
    if parts is None or parts.query or parts.fragment or not origins.is_authorized(target, patterns):
        raise ValueError("alvo não autorizado ou inválido (use uma origem autorizada, sem query string)")
    return target


def host_permission(url: str) -> str:
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}/*"


def server_url(cfg: Config) -> str:
    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host
    return f"http://{host}:{cfg.port}"


PONTUAL_NAME = "BankMonitor — Leitura Pontual"  # distinta da automatica no gerenciador do navegador
PONTUAL_ACTION_TITLE = "BankMonitor: ler agora a aba do alvo (leitura pontual)"  # tooltip do icone (clique = leitura)
PONTUAL_DESCRIPTION = (
    "Lê, sob acionamento manual, o DOM da aba já aberta pelo usuário e envia ao servidor local do "
    "Bank Monitor (sem recarga)."
)


def build_extension(cfg: Config, target: str, interval_min: float | None, pontual: bool = False) -> Path:
    """Escreve `data/extensao-coletor/` (manifest, background.js e config.json); devolve a pasta.

    `pontual` tem de coincidir com a config (`leitura = "pontual"`): extensao automatica nao usa uma
    config pontual e vice-versa. Pontual nao tem alarme: nao aceita intervalo, nao pede a permissao
    `alarms` e nao grava `intervalo_min`.
    """
    if pontual and interval_min is not None:
        raise ValueError("--intervalo não se aplica à leitura pontual (não há alarme)")
    if interval_min is None:
        interval_min = DEFAULT_INTERVAL_MIN
    if pontual and not cfg.browser_pontual:
        raise ValueError('--pontual exige leitura = "pontual" em [coleta_navegador]')
    if cfg.browser_pontual and not pontual:
        raise ValueError("a configuração é de leitura pontual: gere a extensão com --pontual")
    validate_target(target, cfg.browser_origins)
    target = origins.normalize_target(target)
    if not interval_min >= MIN_INTERVAL_MIN:
        raise ValueError("intervalo mínimo: 0,5 minuto")
    if not pontual and not origins.is_loopback_url(target) and interval_min < EXTERNAL_MIN_INTERVAL_MIN:
        raise ValueError(f"intervalo mínimo para origem externa: {EXTERNAL_MIN_INTERVAL_MIN:g} minutos")
    if not TEMPLATE_DIR.is_dir():
        raise FileNotFoundError(f"modelo da extensão não encontrado: {TEMPLATE_DIR}")
    server = server_url(cfg)
    token = load_or_create_token(cfg.data_dir)

    manifest = json.loads((TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = list(dict.fromkeys([host_permission(server), host_permission(target)]))
    config = {"servidor": server, "token": token, "alvo": target, "intervalo_min": interval_min}
    if pontual:
        config.pop("intervalo_min")
        config["pontual"] = True
        manifest["permissions"] = [p for p in manifest["permissions"] if p != "alarms"]
        manifest["name"] = PONTUAL_NAME
        manifest["description"] = PONTUAL_DESCRIPTION
        manifest["action"] = {"default_title": PONTUAL_ACTION_TITLE}  # so o clique manual; nao e permissao

    out = cfg.data_dir / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_DIR / "background.js", out / "background.js")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(out / "config.json", 0o600)
    except OSError:
        pass
    return out
