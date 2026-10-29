"""Mapa nome -> classe de coletor. O ativo e escolhido em `[collector] active`."""

from __future__ import annotations

from bankmon.collectors.base import Clock, Collector
from bankmon.collectors.browser_html import BrowserHtmlCollector
from bankmon.collectors.demo import DemoCollector
from bankmon.collectors.downdetector import DowndetectorCollector
from bankmon.collectors.offline_html import OfflineHtmlCollector

COLLECTORS: dict[str, type[Collector]] = {
    DemoCollector.name: DemoCollector,
    DowndetectorCollector.name: DowndetectorCollector,
    OfflineHtmlCollector.name: OfflineHtmlCollector,
    BrowserHtmlCollector.name: BrowserHtmlCollector,
}


def collector_class(name: str) -> type[Collector]:
    try:
        return COLLECTORS[name]
    except KeyError:
        raise KeyError(f"coletor desconhecido: {name!r}") from None


def create_collector(name: str, *, seed: int, clock: Clock) -> Collector:
    return collector_class(name).from_options(seed=seed, clock=clock)
