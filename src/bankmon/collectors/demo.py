"""Coletor de demonstracao: dados SINTETICOS e deterministicos.

O resultado depende apenas de (seed, instituicao, hora do relogio), entao o mesmo
instante gera sempre o mesmo cenario, o que torna o historico reproduzivel.
"""

from __future__ import annotations

import random
from typing import Sequence

from bankmon.collectors.base import (
    Clock,
    CollectionResult,
    InstitutionRef,
    Observation,
)
from bankmon.timeutil import utc_now

BLOCK_HOURS = 6  # janela em que um episodio de alerta pode ocorrer
NOT_LISTED_COUNT = 2  # quantas instituicoes aparecem como "nao listadas"
FAILURE_RATE = 0.05
SYNTHETIC = "DEMO: dado sintético"


class DemoCollector:
    name = "demo"
    is_demo = True

    def __init__(self, seed: int = 0, clock: Clock = utc_now) -> None:
        self._seed = seed
        self._clock = clock

    @classmethod
    def from_options(cls, *, seed: int, clock: Clock) -> "DemoCollector":
        return cls(seed=seed, clock=clock)

    def collect(self, institutions: Sequence[InstitutionRef]) -> CollectionResult:
        started = self._clock()
        hour = int(started.timestamp() // 3600)

        failure = random.Random(f"{self._seed}|run|{hour}")
        if failure.random() < FAILURE_RATE:
            status = failure.choice(["error", "inaccessible"])
            return CollectionResult(
                collector=self.name,
                is_demo=self.is_demo,
                started_at=started,
                finished_at=self._clock(),
                source_status=status,
                observations=(),
                error_detail=f"{SYNTHETIC}: falha simulada ({status})",
            )

        not_listed = self._not_listed_ids(institutions)
        observations = tuple(
            Observation(
                institution_id=inst.id,
                level="not_listed" if inst.id in not_listed else self._level(inst.id, hour),
                detail=SYNTHETIC,
            )
            for inst in institutions
        )
        return CollectionResult(
            collector=self.name,
            is_demo=self.is_demo,
            started_at=started,
            finished_at=self._clock(),
            source_status="ok",
            observations=observations,
        )

    def _not_listed_ids(self, institutions: Sequence[InstitutionRef]) -> set[str]:
        if len(institutions) < 8:
            return set()
        ranked = sorted(
            institutions,
            key=lambda i: random.Random(f"{self._seed}|{i.id}|listed").random(),
        )
        return {i.id for i in ranked[:NOT_LISTED_COUNT]}

    def _level(self, institution_id: str, hour: int) -> str:
        """Episodios de 2 a 4 h dentro de blocos de 6 h: ~8% critico, ~17% atencao."""
        block, offset = divmod(hour, BLOCK_HOURS)
        rng = random.Random(f"{self._seed}|{institution_id}|{block}")
        roll = rng.random()
        if roll >= 0.25:
            return "no_alert"
        length = rng.randint(2, 4)
        start = rng.randint(0, BLOCK_HOURS - length)
        if not start <= offset < start + length:
            return "no_alert"
        return "critical" if roll < 0.08 else "attention"
