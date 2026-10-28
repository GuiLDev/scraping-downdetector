"""Helpers de tempo: tudo e gravado em UTC (ISO-8601) e exibido em America/Sao_Paulo."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Converte para UTC; datetimes ingenuos sao rejeitados para evitar erros silenciosos."""
    if dt.tzinfo is None:
        raise ValueError("datetime sem fuso horario; use datetimes aware (UTC)")
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """UTC com precisao de segundos, ex.: 2026-10-28T12:00:00Z."""
    return ensure_utc(dt).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value))


def to_local(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(LOCAL_TZ)


def fmt_local(dt: datetime) -> str:
    return to_local(dt).strftime("%d/%m/%Y %H:%M")
