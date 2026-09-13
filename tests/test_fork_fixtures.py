"""The default config reproduces the Factorio fork's numbers exactly.

Each fixture under tests/fixtures/ is one real episode from the fork's
table_runs/20260810T125411 eval logs (samples, ledger events, quota item,
end tick) together with the fork's own ``episode_metrics`` output for it.
Equality is exact (``==`` on floats): the arithmetic is the same code, only
the constants moved into ``ScoringConfig``.
"""

import json
from pathlib import Path

import pytest

from wrench_core.metrics import episode_metrics, fired_events
from wrench_core.scoring import (
    detection_counts,
    detection_metrics,
    floor_adjusted_throughput_retained,
    frozen_baseline,
    recovery_at,
    throughput_retained,
    time_to_recovery_parts,
)

FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures").glob("*.json"))


@pytest.fixture(params=FIXTURES, ids=[p.stem for p in FIXTURES])
def episode(request):
    return json.loads(request.param.read_text())


def test_episode_metrics_is_identical_to_the_fork(episode):
    got = episode_metrics(
        episode["samples"],
        episode["ledger_events"],
        episode["quota_item"],
        episode["end_tick"],
    )
    assert got == episode["expected"]


def test_per_fire_scorers_match_the_fork(episode):
    samples = episode["samples"]
    ledger = episode["ledger_events"]
    end_tick = episode["end_tick"]
    expected = episode["expected"]
    item = expected["item"]
    fires = fired_events(ledger)
    assert len(fires) == expected["num_fires"] >= 1

    for fire, tr_entry, rec_entry, ttr_entry in zip(
        fires,
        expected["throughput_retained"]["metadata"]["fires"],
        expected["recovery"]["metadata"]["fires"],
        expected["time_to_recovery"]["fires"],
    ):
        fire_tick = fire["tick"]
        horizon = end_tick - fire_tick
        assert frozen_baseline(samples, item, fire_tick) == tr_entry["baseline_per_min"]
        assert throughput_retained(samples, item, fire_tick, horizon) == tr_entry["tr"]
        assert (
            floor_adjusted_throughput_retained(samples, item, fire_tick, horizon, fire)
            == tr_entry["floor_tr"]
        )
        assert recovery_at(samples, item, fire_tick, horizon) == rec_entry["recovered"]
        parts = time_to_recovery_parts(samples, item, fire_tick, horizon)
        assert parts == ttr_entry["parts"]

    det = detection_metrics(ledger, fires)
    meta = expected["detection"]["metadata"]
    assert det == {
        k: meta[k] for k in ("latencies", "precision", "precision_strict", "recall")
    }
    counts = detection_counts(ledger, fires)
    assert {k: meta[k] for k in counts} == counts


def test_fixtures_cover_the_interesting_paths():
    """Guard against silently swapping the fixtures for trivial episodes."""
    by_name = {p.stem: json.loads(p.read_text())["expected"] for p in FIXTURES}
    spam = by_name["one_fire_report_spam"]["scalars"]
    assert spam["num_reports"] == 16
    assert spam["detection_precision"] == 1 / 16
    assert spam["detection_recall"] == 0.0  # gated below DETECTION_PRECISION_FLOOR

    gated = by_name["two_fires_gated_recall"]
    assert [f["kind"] for f in gated["throughput_retained"]["metadata"]["fires"]] == [
        "entity_destruction",
        "belt_cut",
    ]
    # belt_cut has no redundancy count: floor-adjusted pool covers one fire.
    assert gated["throughput_retained"]["metadata"]["floor_adjusted_num_fires"] == 1

    redundant = by_name["redundant_two_fires_half_recall"]
    assert redundant["scalars"]["detection_recall"] == 0.5
    assert redundant["scalars"]["throughput_retained_floor_adj"] == -0.5  # winsorized
