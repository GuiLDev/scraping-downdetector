"""Criterios de aceite da E1.1 para status.py (regras de estado, agrupamento e metricas)."""

from datetime import datetime, timedelta, timezone

import pytest

from bankmon import db, runner
from bankmon.config import load_config
from bankmon.status import (
    ATTENTION,
    CRITICAL,
    NO_ALERT,
    NO_DATA,
    NOT_MONITORED,
    SECTION_ALERTS,
    SECTION_CLEAR,
    SECTION_NO_COVERAGE,
    Card,
    RunView,
    build_cards,
    effective_state,
    group_and_sort,
    hourly_alert_counts,
    state_timeline,
)
from conftest import T0, TOML, FakeCollector

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
STALE = timedelta(minutes=90)


def rv(finished_at, status="ok", levels=None, run_id=1):
    return RunView(run_id, finished_at, status, False, levels if levels is not None else {})


# --- 1. Prazo ----------------------------------------------------------------


@pytest.mark.parametrize(
    "level,fresh_state",
    [
        ("no_alert", NO_ALERT),
        ("attention", ATTENTION),
        ("critical", CRITICAL),
        ("not_listed", NOT_MONITORED),
    ],
)
@pytest.mark.parametrize(
    "age,expect_fresh",
    [
        (timedelta(minutes=89), True),
        (timedelta(minutes=90), True),  # exatamente o prazo ainda vale
        (timedelta(minutes=90, seconds=1), False),
        (timedelta(minutes=91), False),
    ],
)
def test_deadline_boundaries_for_every_level(level, fresh_state, age, expect_fresh):
    latest = rv(NOW - age, levels={"x": level})
    expected = fresh_state if expect_fresh else NO_DATA
    assert effective_state("x", latest, NOW, STALE) == expected


def test_not_listed_expired_is_no_data_not_not_monitored():
    """4. `not_listed` vence com o prazo como os demais: depois dele vira Sem dados."""
    latest = rv(NOW - timedelta(minutes=91), levels={"x": "not_listed"})
    assert effective_state("x", latest, NOW, STALE) == NO_DATA
    fresh = rv(NOW - timedelta(minutes=10), levels={"x": "not_listed"})
    assert effective_state("x", fresh, NOW, STALE) == NOT_MONITORED


def test_deadline_follows_configured_value():
    latest = rv(NOW - timedelta(minutes=61), levels={"x": "no_alert"})
    assert effective_state("x", latest, NOW, timedelta(minutes=60)) == NO_DATA
    assert effective_state("x", latest, NOW, timedelta(minutes=61)) == NO_ALERT


def test_future_run_is_not_stale():
    """Relogio levemente atrasado: execucao 'no futuro' nao pode virar Sem dados por prazo."""
    latest = rv(NOW + timedelta(minutes=2), levels={"x": "attention"})
    assert effective_state("x", latest, NOW, STALE) == ATTENTION


# --- 2/3. Falha mais recente e nunca verde sem execucao valida (nivel de banco) ----


def _states(cfg, now):
    conn = db.open_db(cfg)
    try:
        cards = build_cards(db.list_institution_infos(conn), db.latest_run(conn), now, STALE)
    finally:
        conn.close()
    return {c.institution_id: c.state for c in cards}


@pytest.mark.parametrize("status", ["error", "inaccessible", "not_authorized"])
def test_latest_failed_run_overrides_earlier_ok_within_deadline(cfg, status):
    """2. Mesmo havendo uma execucao ok dentro do prazo, a falha mais recente zera tudo."""
    ok_levels = {"inst-1": "critical", "inst-2": "no_alert"}
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels=ok_levels, at=T0))
    assert _states(cfg, T0 + timedelta(minutes=5)) == {"inst-1": CRITICAL, "inst-2": NO_ALERT}

    runner.run_collection(
        cfg, "cli", collector=FakeCollector(status=status, at=T0 + timedelta(minutes=10))
    )
    got = _states(cfg, T0 + timedelta(minutes=11))
    assert got == {"inst-1": NO_DATA, "inst-2": NO_DATA}


def test_recovers_after_failure_with_new_ok_run(cfg):
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "attention"}, at=T0))
    runner.run_collection(
        cfg, "cli", collector=FakeCollector(status="error", at=T0 + timedelta(minutes=10))
    )
    runner.run_collection(
        cfg,
        "cli",
        collector=FakeCollector(
            levels={"inst-1": "no_alert", "inst-2": "no_alert"}, at=T0 + timedelta(minutes=20)
        ),
    )
    assert _states(cfg, T0 + timedelta(minutes=21)) == {"inst-1": NO_ALERT, "inst-2": NO_ALERT}


def test_empty_database_is_never_green(cfg):
    assert _states(cfg, NOW) == {"inst-1": NO_DATA, "inst-2": NO_DATA}


def test_only_failures_is_never_green(cfg):
    for k, status in enumerate(["error", "inaccessible", "not_authorized"]):
        runner.run_collection(
            cfg, "cli", collector=FakeCollector(status=status, at=T0 + timedelta(minutes=k))
        )
    assert set(_states(cfg, T0 + timedelta(minutes=5)).values()) == {NO_DATA}


def test_institution_without_observation_is_no_data(cfg):
    runner.run_collection(cfg, "cli", collector=FakeCollector(levels={"inst-1": "no_alert"}, at=T0))
    assert _states(cfg, T0 + timedelta(minutes=1)) == {"inst-1": NO_ALERT, "inst-2": NO_DATA}


def test_institution_added_to_toml_after_last_run_is_no_data(cfg, write_config):
    runner.run_collection(
        cfg,
        "cli",
        collector=FakeCollector(levels={"inst-1": "no_alert", "inst-2": "no_alert"}, at=T0),
    )
    extra = (
        '\n[[institutions]]\nid = "inst-3"\nname = "Inst 3"\ncategory = "a"\ndd_label = "Inst 3"\n'
    )
    write_config(TOML.format(collector="demo") + extra)
    new_cfg = load_config()
    got = _states(new_cfg, T0 + timedelta(minutes=1))
    assert got == {"inst-1": NO_ALERT, "inst-2": NO_ALERT, "inst-3": NO_DATA}


def test_not_listed_end_to_end_is_not_monitored_then_expires(cfg):
    runner.run_collection(
        cfg,
        "cli",
        collector=FakeCollector(levels={"inst-1": "not_listed", "inst-2": "no_alert"}, at=T0),
    )
    assert _states(cfg, T0 + timedelta(minutes=30)) == {"inst-1": NOT_MONITORED, "inst-2": NO_ALERT}
    assert _states(cfg, T0 + timedelta(minutes=91)) == {"inst-1": NO_DATA, "inst-2": NO_DATA}


# --- 5. group_and_sort -------------------------------------------------------


def card(name, state, order=1):
    return Card(f"id-{name.lower()}", name, "c", f"Cat{order}", order, state)


def test_group_and_sort_always_returns_three_sections_even_when_empty():
    sections = group_and_sort([])
    assert [s.key for s in sections] == [SECTION_ALERTS, SECTION_CLEAR, SECTION_NO_COVERAGE]
    assert [s.title for s in sections] == [
        "Com alerta",
        "Sem alerta identificado",
        "Sem cobertura",
    ]
    assert all(s.cards == () for s in sections)


def test_group_and_sort_missing_buckets_still_present():
    sections = group_and_sort([card("A", NO_ALERT)])
    assert [len(s.cards) for s in sections] == [0, 1, 0]


def test_critical_before_attention_even_with_worse_category_order_and_name():
    cards = [
        card("Aaa", ATTENTION, order=1),
        card("Zzz", CRITICAL, order=9),
        card("Bbb", ATTENTION, order=1),
    ]
    alerts = group_and_sort(cards)[0]
    assert [c.name for c in alerts.cards] == ["Zzz", "Aaa", "Bbb"]


def test_tiebreak_by_category_sort_order_then_name_casefold():
    cards = [
        card("beta", NO_ALERT, order=2),
        card("Zulu", NO_ALERT, order=1),
        card("alfa", NO_ALERT, order=2),
        card("Mike", NO_ALERT, order=1),
        card("Zeca", NO_ALERT, order=3),
    ]
    clear = group_and_sort(cards)[1]
    assert [c.name for c in clear.cards] == ["Mike", "Zulu", "alfa", "beta", "Zeca"]


def test_no_coverage_groups_no_data_before_not_monitored():
    cards = [card("Aaa", NOT_MONITORED), card("Zzz", NO_DATA)]
    assert [c.name for c in group_and_sort(cards)[2].cards] == ["Zzz", "Aaa"]


def test_group_and_sort_does_not_lose_or_duplicate_cards():
    states = [CRITICAL, ATTENTION, NO_ALERT, NO_DATA, NOT_MONITORED] * 4
    cards = [card(f"N{i}", s, order=i % 3) for i, s in enumerate(states)]
    sections = group_and_sort(cards)
    flat = [c.institution_id for s in sections for c in s.cards]
    assert sorted(flat) == sorted(c.institution_id for c in cards)


def test_group_and_sort_accepts_iterators_and_ignores_input_order():
    cards = [card("B", NO_ALERT), card("A", NO_ALERT)]
    assert group_and_sort(iter(cards)) == group_and_sort(reversed(cards))


# --- 11. Metricas --------------------------------------------------------------


def test_hourly_has_data_false_for_hours_without_ok_run():
    start = NOW - timedelta(hours=4)
    runs = [
        rv(NOW - timedelta(hours=4) + timedelta(minutes=10),
           levels={"a": "critical", "b": "attention"}, run_id=1),
        rv(NOW - timedelta(hours=3) + timedelta(minutes=5), status="error", run_id=2),
        rv(NOW - timedelta(hours=2) + timedelta(minutes=5), status="inaccessible", run_id=3),
        rv(NOW - timedelta(hours=1) + timedelta(minutes=5), status="not_authorized", run_id=4),
    ]
    buckets = hourly_alert_counts(runs, start, NOW)
    assert [b.has_data for b in buckets] == [True, False, False, False]
    assert (buckets[0].critical, buckets[0].attention) == (1, 1)
    # horas sem ok trazem 0 (mas has_data=False: nao significam "sem alerta")
    assert all((b.attention, b.critical) == (0, 0) for b in buckets[1:])


def test_hourly_hours_are_contiguous_aligned_and_half_open():
    start = datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    buckets = hourly_alert_counts([], start, end)
    assert [b.hour.hour for b in buckets] == [8, 9, 10, 11]  # `end` exclusivo
    assert all(b.hour.minute == 0 for b in buckets)
    assert not any(b.has_data for b in buckets)


def test_hourly_empty_range():
    assert hourly_alert_counts([], NOW, NOW) == []
    assert hourly_alert_counts([], NOW, NOW - timedelta(hours=1)) == []


def test_hourly_ok_then_error_in_same_hour_uses_ok_run():
    h = NOW - timedelta(hours=1)
    runs = [
        rv(h.replace(minute=5), levels={"a": "critical"}, run_id=1),
        rv(h.replace(minute=40), status="error", run_id=2),
    ]
    (b,) = hourly_alert_counts(runs, h.replace(minute=0), NOW.replace(minute=0))
    assert b.has_data and b.critical == 1


def test_hourly_institution_filter_and_ignores_no_alert_and_not_listed():
    h = NOW - timedelta(hours=1)
    runs = [
        rv(
            h.replace(minute=5),
            levels={"a": "critical", "b": "attention", "c": "no_alert", "d": "not_listed"},
        )
    ]
    s, e = h.replace(minute=0), NOW.replace(minute=0)
    (all_,) = hourly_alert_counts(runs, s, e)
    (only_b,) = hourly_alert_counts(runs, s, e, ["b"])
    (none,) = hourly_alert_counts(runs, s, e, [])
    assert (all_.attention, all_.critical) == (1, 1)
    assert (only_b.attention, only_b.critical) == (1, 0)
    assert (none.attention, none.critical, none.has_data) == (0, 0, True)


def test_hourly_unsorted_input_picks_latest_ok_of_the_hour():
    h = NOW - timedelta(hours=1)
    late = rv(h.replace(minute=50), levels={"a": "no_alert"}, run_id=2)
    early = rv(h.replace(minute=10), levels={"a": "critical"}, run_id=1)
    (b,) = hourly_alert_counts([late, early], h.replace(minute=0), NOW.replace(minute=0))
    assert (b.attention, b.critical) == (0, 0) and b.has_data


def _latest_at(runs, t):
    eligible = [r for r in runs if r.finished_at <= t]
    return max(eligible, key=lambda r: (r.finished_at, r.run_id)) if eligible else None


def test_state_timeline_is_consistent_with_effective_state():
    """Amostra a cada 5 min (+30 s, fora das fronteiras) e compara com effective_state."""
    base = NOW - timedelta(hours=24)
    spec = [  # (minutos desde base, status, nivel)
        (10, "ok", "attention"),
        (70, "ok", "critical"),
        (130, "ok", "no_alert"),
        (140, "error", None),
        (200, "ok", "not_listed"),
        (500, "ok", "attention"),  # lacuna longa antes: no_data no meio
        (560, "inaccessible", None),
        (600, "ok", "no_alert"),
        (900, "not_authorized", None),
        (1300, "ok", "critical"),
    ]
    runs = [
        rv(base + timedelta(minutes=m), status=s, levels={"x": lvl} if lvl else {}, run_id=i)
        for i, (m, s, lvl) in enumerate(spec, 1)
    ]
    start, end = base, NOW
    segments = state_timeline("x", runs, start, end, STALE)

    # cobertura contigua e sem sobreposicao de [start, end), sem vizinhos iguais
    assert segments[0].start == start and segments[-1].end == end
    for a, b in zip(segments, segments[1:]):
        assert a.end == b.start
        assert a.state != b.state
    assert all(s.start < s.end for s in segments)

    def timeline_state(t):
        (seg,) = [s for s in segments if s.start <= t < s.end]
        return seg.state

    t = start + timedelta(seconds=30)
    checked = 0
    while t < end:
        expected = effective_state("x", _latest_at(runs, t), t, STALE)
        assert timeline_state(t) == expected, f"divergencia em {t.isoformat()}"
        t += timedelta(minutes=5)
        checked += 1
    assert checked > 250


def test_state_timeline_gap_after_deadline_is_no_data():
    run1 = rv(NOW - timedelta(hours=5), levels={"x": "attention"}, run_id=1)
    segs = state_timeline("x", [run1], NOW - timedelta(hours=5), NOW, STALE)
    assert [(s.state, s.end - s.start) for s in segs] == [
        (ATTENTION, STALE),
        (NO_DATA, timedelta(hours=5) - STALE),
    ]


def test_state_timeline_failed_run_cuts_validity_of_previous_ok():
    ok = rv(NOW - timedelta(minutes=60), levels={"x": "critical"}, run_id=1)
    bad = rv(NOW - timedelta(minutes=30), status="error", run_id=2)
    segs = state_timeline("x", [ok, bad], NOW - timedelta(minutes=60), NOW, STALE)
    assert [(s.state, s.end - s.start) for s in segs] == [
        (CRITICAL, timedelta(minutes=30)),
        (NO_DATA, timedelta(minutes=30)),
    ]


def test_state_timeline_includes_previous_run_before_start():
    before = rv(NOW - timedelta(hours=10), levels={"x": "critical"})
    # a execucao anterior a `start` ja expirou -> tudo no_data
    segs = state_timeline("x", [before], NOW - timedelta(hours=2), NOW, STALE)
    assert [s.state for s in segs] == [NO_DATA]
    recent = rv(NOW - timedelta(minutes=130), levels={"x": "critical"})
    segs = state_timeline("x", [recent], NOW - timedelta(minutes=100), NOW, STALE)
    assert [s.state for s in segs] == [CRITICAL, NO_DATA]
    assert segs[0].start == NOW - timedelta(minutes=100)


def test_state_timeline_institution_missing_in_run_is_no_data():
    run1 = rv(NOW - timedelta(minutes=30), levels={"outra": "critical"})
    segs = state_timeline("x", [run1], NOW - timedelta(minutes=30), NOW, STALE)
    assert [s.state for s in segs] == [NO_DATA]
