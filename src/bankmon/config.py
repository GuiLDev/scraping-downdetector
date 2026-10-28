"""Carrega e valida o TOML de configuracao.

Variaveis de ambiente:
  BANKMON_CONFIG    caminho do TOML (padrao: <raiz>/config/institutions.toml)
  BANKMON_DATA_DIR  diretorio de dados (padrao: <raiz>/data)
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from bankmon.collectors.registry import COLLECTORS, collector_class

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "institutions.toml"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_STALE_MINUTES = 1440


class ConfigError(Exception):
    """Configuracao invalida; a mensagem e voltada ao usuario."""


@dataclass(frozen=True)
class CategoryConfig:
    id: str
    name: str
    order: int


@dataclass(frozen=True)
class InstitutionConfig:
    id: str
    name: str
    category: str
    dd_label: str
    enabled: bool


@dataclass(frozen=True)
class Config:
    config_path: Path
    data_dir: Path
    collector: str
    interval_minutes: int
    stale_after_minutes: int
    host: str
    port: int
    demo_seed: int
    categories: tuple[CategoryConfig, ...]
    institutions: tuple[InstitutionConfig, ...]

    @property
    def is_demo(self) -> bool:
        return collector_class(self.collector).is_demo

    @property
    def db_path(self) -> Path:
        return self.data_dir / ("bankmon-demo.db" if self.is_demo else "bankmon.db")

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "bankmon.lock"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def resolve_config_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("BANKMON_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


def resolve_data_dir(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("BANKMON_DATA_DIR")
    return Path(env) if env else PROJECT_ROOT / "data"


def load_config(
    path: Path | str | None = None, data_dir: Path | str | None = None
) -> Config:
    cfg_path = resolve_config_path(path)
    try:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"arquivo de configuração não encontrado: {cfg_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML inválido em {cfg_path}: {exc}") from None

    collector = _get(raw, "collector", "active", str)
    if collector not in COLLECTORS:
        raise ConfigError(
            f"coletor desconhecido: {collector!r} (disponíveis: {', '.join(sorted(COLLECTORS))})"
        )

    interval = _get(raw, "schedule", "interval_minutes", int)
    stale_after = _get(raw, "schedule", "stale_after_minutes", int)
    if interval <= 0:
        raise ConfigError("schedule.interval_minutes deve ser maior que zero")
    max_stale = min(4 * interval, MAX_STALE_MINUTES)
    if not interval <= stale_after <= max_stale:
        raise ConfigError(
            f"schedule.stale_after_minutes ({stale_after}) deve ficar entre "
            f"interval_minutes ({interval}) e {max_stale}"
        )

    host = _get(raw, "server", "host", str)
    port = _get(raw, "server", "port", int)
    if not _is_loopback_ip(host):
        raise ConfigError(
            f"server.host deve ser um IP literal de loopback (ex.: 127.0.0.1 ou ::1); "
            f"recebido {host!r}"
        )
    if not 1 <= port <= 65535:
        raise ConfigError(f"server.port fora do intervalo 1-65535: {port}")

    seed = _get(raw, "demo", "seed", int)
    categories = _parse_categories(raw.get("categories"))
    institutions = _parse_institutions(raw.get("institutions"), {c.id for c in categories})

    return Config(
        config_path=cfg_path,
        data_dir=resolve_data_dir(data_dir),
        collector=collector,
        interval_minutes=interval,
        stale_after_minutes=stale_after,
        host=host,
        port=port,
        demo_seed=seed,
        categories=categories,
        institutions=institutions,
    )


def _is_loopback_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _table_list(raw, name: str) -> list[dict]:
    if not raw:
        raise ConfigError(f"nenhuma [[{name}]] definida")
    if not isinstance(raw, list) or not all(isinstance(i, dict) for i in raw):
        raise ConfigError(f"{name} deve ser uma lista de tabelas ([[{name}]])")
    return raw


def _get(raw: dict, section: str, key: str, typ: type):
    value = raw.get(section, {}).get(key) if isinstance(raw.get(section), dict) else None
    if value is None:
        raise ConfigError(f"campo obrigatório ausente: [{section}] {key}")
    # bool e subclasse de int; nao aceitar true onde se espera numero.
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise ConfigError(f"[{section}] {key} deve ser {typ.__name__}")
    return value


def _field(item: dict, key: str, typ: type, where: str):
    value = item.get(key)
    if value is None:
        raise ConfigError(f"{where}: campo obrigatório ausente: {key}")
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise ConfigError(f"{where}: {key} deve ser {typ.__name__}")
    return value


def _check_slug(value: str, where: str) -> None:
    if not SLUG_RE.match(value):
        raise ConfigError(f"{where}: id inválido {value!r} (use minúsculas, dígitos, '-' e '_')")


def _parse_categories(raw) -> tuple[CategoryConfig, ...]:
    result, seen = [], set()
    for idx, item in enumerate(_table_list(raw, "categories"), 1):
        where = f"categories #{idx}"
        cid = _field(item, "id", str, where)
        _check_slug(cid, where)
        if cid in seen:
            raise ConfigError(f"categoria duplicada: {cid!r}")
        seen.add(cid)
        result.append(
            CategoryConfig(cid, _field(item, "name", str, where), _field(item, "order", int, where))
        )
    return tuple(result)


def _parse_institutions(raw, category_ids: set[str]) -> tuple[InstitutionConfig, ...]:
    result, seen = [], set()
    for idx, item in enumerate(_table_list(raw, "institutions"), 1):
        where = f"institutions #{idx}"
        iid = _field(item, "id", str, where)
        _check_slug(iid, where)
        if iid in seen:
            raise ConfigError(f"instituição duplicada: {iid!r}")
        seen.add(iid)
        category = _field(item, "category", str, where)
        if category not in category_ids:
            raise ConfigError(f"instituição {iid!r} usa categoria inexistente: {category!r}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}: enabled deve ser true/false")
        result.append(
            InstitutionConfig(
                id=iid,
                name=_field(item, "name", str, where),
                category=category,
                dd_label=_field(item, "dd_label", str, where),
                enabled=enabled,
            )
        )
    return tuple(result)
