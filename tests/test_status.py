from datetime import datetime, timedelta, timezone

import pytest

from bankmon.status import (
    ATTENTION,
    CRITICAL,
    NO_ALERT,
    NO_DATA,
    NOT_MONITORED,
    Card,
    RunView,
    effective_state,
    group_and_sort,
    hourly_alert_counts,
    state_timeline,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
STALE = timedelta(minutes=90)


def run(minutes_ago, status="ok", levels=None, run_id=1):
    return RunView(
        run_id, NOW - timedelta(minutes=minutes_ago), status, False, levels or {"x": "no_alert"}
    )


@pytest.mark.parametrize(
    "minutes,expected", [(89, NO_ALERT), (90, NO_ALERT), (91, NO_DATA)]
)
def test_stale_boundary(minutes, expected):
    assert effective_state("x", run(minutes), NOW, STALE) == expected


@pytest.mark.parametrize("status", ["inaccessible", "error", "not_authorized"])
def test_failed_latest_run_means_no_data(status):
    latest = run(5, status=status, levels={})
    assert effective_state("x", latest, NOW, STALE) == NO_DATA


def test_never_green_without_valid_run():
    assert effective_state("x", None, NOW, STALE) == NO_DATA


def test_levels_mapping_and_missing_observation():
    latest = run(
        1, levels={"c": "critical", "a": "attention", "n": "not_listed", "g": "no_alert"}
    )
    assert effective_state("c", latest, NOW, STALE) == CRITICAL
    assert effective_state("a", latest, NOW, STALE) == ATTENTION
    assert effective_state("n", latest, NOW, STALE) == NO_DATA  # ausente da pagina: sem dados
    assert effective_state("g", latest, NOW, STALE) == NO_ALERT
    assert effective_state("ausente", latest, NOW, STALE) == NO_DATA


def card(name, state, order=1):
    return Card(name.lower(), name, "c", "C", order, state)


def test_group_and_sort():
    cards = [
        card("Zeta", NO_ALERT),
        card("Beta", ATTENTION, order=1),
        card("Alfa", CRITICAL, order=2),
        card("Gama", NOT_MONITORED),
        card("Delta", NO_DATA),
        card("Aaa", ATTENTION, order=2),
    ]
    alerts, clear, coverage = group_and_sort(cards)
    assert [c.name for c in alerts.cards] == ["Alfa", "Beta", "Aaa"]
    assert [c.name for c in clear.cards] == ["Zeta"]
    assert [c.name for c in coverage.cards] == ["Delta", "Gama"]


def test_hourly_counts():
    start = NOW - timedelta(hours=3)
    runs = [
        run(150, levels={"a": "attention", "b": "critical"}, run_id=1),
        run(140, levels={"a": "attention", "b": "no_alert"}, run_id=2),  # mesma hora, vale a ultima
        run(30, status="error", levels={}, run_id=3),
    ]
    buckets = hourly_alert_counts(runs, start, NOW)
    assert len(buckets) == 3
    assert (buckets[0].attention, buckets[0].critical, buckets[0].has_data) == (1, 0, True)
    assert buckets[2].has_data is False


def test_timeline_gaps_are_no_data():
    start = NOW - timedelta(hours=6)
    runs = [
        RunView(1, NOW - timedelta(hours=5), "ok", False, {"x": "critical"}),
        RunView(2, NOW - timedelta(hours=2), "ok", False, {"x": "no_alert"}),
    ]
    segs = state_timeline("x", runs, start, NOW, STALE)
    assert [(s.state, s.start, s.end) for s in segs] == [
        (NO_DATA, start, NOW - timedelta(hours=5)),
        (CRITICAL, NOW - timedelta(hours=5), NOW - timedelta(hours=5) + STALE),
        (NO_DATA, NOW - timedelta(hours=5) + STALE, NOW - timedelta(hours=2)),
        (NO_ALERT, NOW - timedelta(hours=2), NOW - timedelta(hours=2) + STALE),
        (NO_DATA, NOW - timedelta(hours=2) + STALE, NOW),
    ]


def test_timeline_without_runs():
    segs = state_timeline("x", [], NOW - timedelta(hours=1), NOW, STALE)
    assert [s.state for s in segs] == [NO_DATA]
