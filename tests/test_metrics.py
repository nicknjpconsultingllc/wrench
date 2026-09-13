"""Unit tests for wrench_core.metrics.episode_metrics on canned data (ported
from the fork's tests/wrench/test_episode.py::TestEpisodeMetrics)."""

import pytest

from wrench_core.metrics import episode_metrics
from wrench_core.scoring import (
    detection_metrics,
    recovery_at,
    throughput_retained,
    winsorize_tr,
)

ITEM = "iron-plate"
STEP = 41  # engine sample interval


def _samples(rate_before, rate_after, fire_tick, end_tick):
    """Cumulative production at ~41-tick intervals with a rate change at
    fire_tick (rates in items/min)."""
    samples = []
    count = 0.0
    tick = 0
    while tick <= end_tick:
        rate = rate_before if tick < fire_tick else rate_after
        samples.append({"tick": tick, "counts": {ITEM: count}})
        count += rate * STEP / 3600
        tick += STEP
    return samples


def _fire(tick, x=2.0, y=0.0, seed=11, kind="entity_destruction"):
    return {
        "tick": tick,
        "event": "fired",
        "kind": kind,
        "seed": seed,
        # same_type_total rides on the manifest entry (see
        # scoring._redundancy_total), never in detail.
        "affected": [{"name": "stone-furnace", "x": x, "y": y, "same_type_total": 2}],
        "detail": {},
    }


def _report(tick, x, y):
    return {
        "tick": tick,
        "event": "report_fault",
        "kind": None,
        "seed": None,
        "affected": [],
        "detail": {"x": x, "y": y, "cause": "furnace gone"},
    }


class TestEpisodeMetrics:
    def test_unscoreable_when_nothing_fired(self):
        samples = _samples(40, 40, fire_tick=10**9, end_tick=20_000)
        m = episode_metrics(samples, [], ITEM, end_tick=20_000)
        assert m["num_fires"] == 0
        assert m["throughput_retained"]["value"] is None
        assert m["throughput_retained"]["metadata"]["scoreable"] is False
        assert m["recovery"]["value"] is None
        assert m["time_to_recovery"]["mean_ticks"] is None
        # Vacuous detection: nothing to find, nothing hallucinated.
        assert m["detection"]["value"] == 1.0
        assert m["detection"]["metadata"]["precision_strict"] == 1.0
        s = m["scalars"]
        assert s["throughput_retained"] is None
        assert s["tr_scoreable"] is False
        assert s["tr_pooled_denominator"] == 0.0
        assert s["detection_recall"] == 1.0

    def test_matches_scoring_functions_on_a_half_loss(self):
        fire_tick = 12_000
        end_tick = fire_tick + 6_000
        samples = _samples(40, 20, fire_tick, end_tick)
        fire = _fire(fire_tick)
        ledger = [{"tick": 1, "event": "armed", "kind": "entity_destruction"}, fire]

        m = episode_metrics(samples, ledger, ITEM, end_tick)
        horizon = end_tick - fire_tick
        expected_tr = throughput_retained(samples, ITEM, fire_tick, horizon)
        assert expected_tr is not None
        assert m["throughput_retained"]["value"] == pytest.approx(expected_tr)
        assert m["throughput_retained"]["value"] == pytest.approx(0.5, abs=0.05)
        meta = m["throughput_retained"]["metadata"]
        assert meta["scoreable"] is True
        assert meta["num_fires"] == 1
        assert meta["fires"][0]["horizon_ticks"] == horizon
        assert winsorize_tr(meta["pooled_numerator"] / meta["pooled_denominator"]) == (
            pytest.approx(expected_tr)
        )
        # Floor-adjusted pool is defined: entity_destruction with same_type_total.
        assert meta["floor_adjusted_scoreable"] is True
        assert meta["floor_adjusted_num_fires"] == 1

        assert m["recovery"]["value"] == float(
            recovery_at(samples, ITEM, fire_tick, horizon)
        )
        assert m["recovery"]["value"] == 0.0
        ttr = m["time_to_recovery"]
        assert ttr["fires"][0]["parts"]["recovered"] is False
        assert ttr["mean_ticks"] == float(horizon)  # right-censored at budget

        s = m["scalars"]
        assert s["throughput_retained_raw"] == pytest.approx(
            meta["pooled_numerator"] / meta["pooled_denominator"]
        )
        assert s["recovery_rate"] == 0.0
        assert s["num_fires"] == 1

    def test_recovery_and_detection(self):
        fire_tick = 12_000
        recover_tick = fire_tick + 1_500
        end_tick = fire_tick + 6_000
        # Full rate before, none for 1500 ticks, then full rate again.
        samples = []
        count = 0.0
        tick = 0
        while tick <= end_tick:
            rate = 0 if fire_tick <= tick < recover_tick else 40
            samples.append({"tick": tick, "counts": {ITEM: count}})
            count += rate * STEP / 3600
            tick += STEP
        fire = _fire(fire_tick, x=2.0, y=0.0)
        ledger = [
            fire,
            _report(fire_tick + 300, 2.5, 0.0),
            _report(fire_tick + 400, 9.0, 0.0),
        ]

        m = episode_metrics(samples, ledger, ITEM, end_tick)
        assert m["recovery"]["value"] == 1.0
        ttr = m["time_to_recovery"]["fires"][0]["parts"]
        assert ttr["recovered"] is True
        assert 0 < ttr["ticks"] < 6_000
        assert m["scalars"]["time_to_recovery_ticks"] == ttr["ticks"]

        det = detection_metrics(ledger, [fire])
        assert m["detection"]["metadata"]["recall"] == det["recall"] == 1.0
        assert m["detection"]["metadata"]["precision"] == det["precision"]
        # Second report is 7 tiles off: inside the loose radius, outside strict.
        assert m["detection"]["metadata"]["precision_strict"] == 0.5
        assert m["detection"]["metadata"]["mean_latency_ticks"] == 300.0
        assert m["scalars"]["detection_latency_ticks"] == 300.0

    def test_tracked_item_comes_from_samples(self):
        samples = [{"tick": 0, "counts": {"copper-cable": 0}}]
        m = episode_metrics(samples, [], "iron-plate", 0)
        assert m["item"] == "copper-cable"
        assert episode_metrics([], [], "iron-plate", 0)["item"] == "iron-plate"
