"""ScoringConfig: the three substrate knobs behave as specified.

- ``ticks_per_minute`` (with the window lengths) lets a wall-clock substrate
  score millisecond ticks and get the same per-minute numbers.
- ``matcher`` replaces Euclidean position matching with any
  ``(report, fire, scope) -> bool`` predicate.
- ``redundancy_kinds`` decides which fire kinds carry a floor-adjustable
  ``same_type_total``.
"""

import pytest

from wrench_core.ledger import LedgerEntry
from wrench_core.scoring import (
    DEFAULT_CONFIG,
    MatchScope,
    ScoringConfig,
    detection_counts,
    detection_metrics,
    floor_adjusted_throughput_retained_parts,
    frozen_baseline,
    position_matcher,
    recovery_at,
    recovery_potential,
    throughput_retained,
    throughput_retained_parts,
    throughput_series,
    time_to_recovery_parts,
)

ITEM = "orders"


def make_samples(segments, interval, ticks_per_minute):
    """Piecewise-constant rates (items/min) sampled every ``interval`` ticks."""
    samples = []
    tick = 0
    count = 0.0
    for duration, rate in segments:
        end = tick + duration
        while tick <= end:
            samples.append({"tick": tick, "counts": {ITEM: count}})
            step = min(interval, end - tick) or interval
            count += rate * step / ticks_per_minute
            tick += interval
    return samples


class TestDefaultIsFactorio:
    def test_default_config_values(self):
        assert DEFAULT_CONFIG == ScoringConfig()
        assert DEFAULT_CONFIG.ticks_per_minute == 3600
        assert DEFAULT_CONFIG.trailing_window_ticks == 1800
        assert DEFAULT_CONFIG.baseline_window_ticks == 3600
        assert DEFAULT_CONFIG.matcher is position_matcher
        assert DEFAULT_CONFIG.redundancy_kinds == frozenset({"entity_destruction"})

    def test_explicit_default_config_is_a_no_op(self):
        samples = make_samples([(6027, 40.0), (1800, 0.0), (7200, 40.0)], 41, 3600)
        plain = throughput_retained(samples, ITEM, 6027, 3600)
        assert plain == throughput_retained(
            samples, ITEM, 6027, 3600, config=ScoringConfig()
        )
        assert throughput_series(samples, ITEM) == throughput_series(
            samples, ITEM, 1800, config=ScoringConfig()
        )


# A compose-style episode: one sample per second, ticks in milliseconds.
MS = ScoringConfig(
    ticks_per_minute=60_000,
    trailing_window_ticks=30_000,
    baseline_window_ticks=60_000,
)
FIRE_MS = 120_000  # two minutes in


@pytest.fixture
def ms_drop_and_recovery():
    """40/min for two minutes, dead for 30 s, then back to 40/min."""
    return make_samples([(FIRE_MS, 40.0), (30_000, 0.0), (120_000, 40.0)], 1000, 60_000)


class TestTicksPerMinute:
    def test_ms_baseline_is_in_items_per_minute(self, ms_drop_and_recovery):
        assert frozen_baseline(ms_drop_and_recovery, ITEM, FIRE_MS, config=MS) == (
            pytest.approx(40.0, rel=0.05)
        )
        # The Factorio default reads the same stream as 3600 ticks/min:
        # 40 items per 60 000 ticks is 2.4 items per 3600 ticks.
        assert frozen_baseline(ms_drop_and_recovery, ITEM, FIRE_MS, 60_000) == (
            pytest.approx(2.4, rel=0.05)
        )

    def test_ms_throughput_retained(self, ms_drop_and_recovery):
        # 30 s dead then full rate over a 60 s horizon -> 0.5.
        tr = throughput_retained(ms_drop_and_recovery, ITEM, FIRE_MS, 60_000, config=MS)
        assert tr == pytest.approx(0.5, abs=0.1)
        # TR is a ratio of like units, so it does not depend on the tick
        # rate once the windows are set; the parts do.
        actual, expected = throughput_retained_parts(
            ms_drop_and_recovery, ITEM, FIRE_MS, 60_000, config=MS
        )
        assert expected == pytest.approx(40.0, rel=0.05)  # 40/min x 1 min

    def test_ms_recovery_uses_the_configured_windows(self, ms_drop_and_recovery):
        # Dead 30 s, then the 30 s trailing window has to refill: recovery
        # lands after ~60 s, inside a 120 s budget.
        assert (
            recovery_at(ms_drop_and_recovery, ITEM, FIRE_MS, 120_000, config=MS) is True
        )
        parts = time_to_recovery_parts(
            ms_drop_and_recovery, ITEM, FIRE_MS, 120_000, config=MS
        )
        assert parts["recovered"] is True
        assert 30_000 < parts["ticks"] < 120_000
        # With Factorio's 1800-tick trailing window (1.8 s here) the same
        # stream "recovers" the moment two post-fire seconds pass at rate.
        assert (
            time_to_recovery_parts(ms_drop_and_recovery, ITEM, FIRE_MS, 120_000)[
                "ticks"
            ]
            < parts["ticks"]
        )

    def test_ms_recovery_potential(self, ms_drop_and_recovery):
        assert (
            recovery_potential(ms_drop_and_recovery, ITEM, FIRE_MS, FIRE_MS, config=MS)
            == 0.0
        )
        end = ms_drop_and_recovery[-1]["tick"]
        assert recovery_potential(
            ms_drop_and_recovery, ITEM, FIRE_MS, end, config=MS
        ) == pytest.approx(1.0, abs=0.05)

    def test_same_episode_scores_the_same_at_either_tick_rate(self):
        """One physical episode, once at 60 ticks/s and once in ms, gives the
        same per-minute baseline and the same TR."""
        game = make_samples([(6000, 40.0), (1800, 0.0), (7200, 40.0)], 60, 3600)
        wall = make_samples(
            [(100_000, 40.0), (30_000, 0.0), (120_000, 40.0)], 1000, 60_000
        )
        assert frozen_baseline(game, ITEM, 6000) == pytest.approx(
            frozen_baseline(wall, ITEM, 100_000, config=MS)
        )
        assert throughput_retained(game, ITEM, 6000, 3600) == pytest.approx(
            throughput_retained(wall, ITEM, 100_000, 60_000, config=MS)
        )


# --- custom matcher ---------------------------------------------------------

DEPENDS_ON = {"api": {"db", "cache"}, "worker": {"db"}, "db": set(), "cache": set()}


def service_matcher(report, fire_event, scope: MatchScope) -> bool:
    """strict: the report names the killed service; loose: that, or a
    service one dependency hop away in either direction."""
    reported = report["detail"].get("service")
    for entry in fire_event["affected"]:
        victim = entry["service"]
        if reported == victim:
            return True
        if not scope.strict and (
            victim in DEPENDS_ON.get(reported, ()) or reported in DEPENDS_ON[victim]
        ):
            return True
    return False


SERVICES = ScoringConfig(matcher=service_matcher)


def kill(tick, service):
    return {
        "tick": tick,
        "event": "fired",
        "kind": "container_kill",
        "seed": 1,
        "affected": [{"service": service}],
        "detail": {},
    }


def report(tick, service):
    return {"tick": tick, "event": "report_fault", "detail": {"service": service}}


class TestCustomMatcher:
    def test_exact_service_matches_both_passes(self):
        fire = kill(1000, "db")
        c = detection_counts([fire, report(1500, "db")], [fire], config=SERVICES)
        assert c["matched_reports"] == 1
        assert c["matched_reports_strict"] == 1
        assert c["latencies"] == [500]

    def test_one_hop_matches_loose_only(self):
        fire = kill(1000, "db")
        ledger = [fire, report(1500, "api")]  # api depends on db
        c = detection_counts(ledger, [fire], config=SERVICES)
        assert c["matched_fires"] == 1
        assert c["matched_reports"] == 1
        assert c["matched_reports_strict"] == 0
        m = detection_metrics(ledger, [fire], config=SERVICES)
        assert m["precision"] == 1.0
        assert m["precision_strict"] == 0.0
        assert m["recall"] == 1.0

    def test_unrelated_service_matches_neither(self):
        fire = kill(1000, "db")
        m = detection_metrics([fire, report(1500, "cache")], [fire], config=SERVICES)
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0

    def test_tick_ordering_is_enforced_outside_the_matcher(self):
        fire = kill(1000, "db")
        m = detection_metrics([report(900, "db"), fire], [fire], config=SERVICES)
        assert m["latencies"] == []
        assert m["recall"] == 0.0

    def test_scope_passed_to_matcher(self):
        seen = []

        def spy(report, fire_event, scope):
            seen.append(scope)
            return False

        fire = kill(1000, "db")
        detection_counts(
            [fire, report(1500, "db")],
            [fire],
            radius=7.0,
            config=ScoringConfig(matcher=spy),
        )
        assert set(seen) == {
            MatchScope(radius=7.0, strict=False),
            MatchScope(radius=3.0, strict=True),
        }

    def test_bipartite_cap_still_applies(self):
        fire = kill(1000, "db")
        spam = [report(1100 + 10 * i, "db") for i in range(10)]
        m = detection_metrics([fire, *spam], [fire], config=SERVICES)
        assert m["precision"] == pytest.approx(0.1)
        assert m["recall"] == 0.0  # gated

    def test_position_matcher_ignores_service_ledgers(self):
        fire = kill(1000, "db")
        m = detection_metrics([fire, report(1500, "db")], [fire])
        assert m["recall"] == 0.0

    def test_position_matcher_accepts_ledger_entries(self):
        fire = LedgerEntry(
            tick=1000,
            event="fired",
            kind="entity_destruction",
            affected=[{"x": 0, "y": 0}],
        )
        rep = LedgerEntry(tick=1200, event="report_fault", detail={"x": 2.0, "y": 0.0})
        assert position_matcher(rep, fire, MatchScope(radius=3.0, strict=True))
        assert not position_matcher(rep, fire, MatchScope(radius=1.0, strict=True))


# --- redundancy kinds -------------------------------------------------------


class TestRedundancyKinds:
    @pytest.fixture
    def samples(self):
        return make_samples([(6027, 40.0), (3600, 20.0)], 41, 3600)

    def _fire(self, kind):
        return {
            "tick": 6027,
            "event": "fired",
            "kind": kind,
            "affected": [{"service": "db", "same_type_total": 2}],
        }

    def test_default_allowlist_is_entity_destruction_only(self, samples):
        assert (
            floor_adjusted_throughput_retained_parts(
                samples, ITEM, 6027, 3600, self._fire("container_kill")
            )
            is None
        )
        assert (
            floor_adjusted_throughput_retained_parts(
                samples, ITEM, 6027, 3600, self._fire("entity_destruction")
            )
            is not None
        )

    def test_opting_a_kind_in(self, samples):
        config = ScoringConfig(redundancy_kinds=frozenset({"container_kill"}))
        parts = floor_adjusted_throughput_retained_parts(
            samples, ITEM, 6027, 3600, self._fire("container_kill"), config=config
        )
        assert parts is not None
        actual, expected = parts
        # Two replicas, one killed, no-op agent: the surviving half is the
        # floor, so the adjusted numerator is ~0.
        assert actual == pytest.approx(0.0, abs=1.0)
        assert expected == pytest.approx(20.0, abs=1.0)
        # And the opted-in allowlist no longer covers Factorio's kind.
        assert (
            floor_adjusted_throughput_retained_parts(
                samples,
                ITEM,
                6027,
                3600,
                self._fire("entity_destruction"),
                config=config,
            )
            is None
        )
