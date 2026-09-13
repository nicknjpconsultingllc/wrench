"""Kaplan-Meier over right-censored (ticks, recovered) pairs."""

import pytest

from wrench_core.survival import (
    SurvivalCurve,
    kaplan_meier,
    pooled_time_to_recovery,
    recovery_data,
)

# Five fires: recovered at 1, censored at 2, recovered at 3 and 4, censored
# at 5. By hand:
#   t=1: n=5, d=1 -> S = 4/5 = 0.8
#   t=3: n=3, d=1 -> S = 0.8 * 2/3 = 0.5333...
#   t=4: n=2, d=1 -> S = 0.5333 * 1/2 = 0.2666...
HAND = [(1, True), (2, False), (3, True), (4, True), (5, False)]


class TestKaplanMeier:
    def test_hand_computed_curve(self):
        km = kaplan_meier(HAND)
        assert km.times == (1.0, 3.0, 4.0)
        assert km.at_risk == (5, 3, 2)
        assert km.events == (1, 1, 1)
        assert km.survival == pytest.approx((0.8, 0.8 * 2 / 3, 0.8 * 2 / 3 / 2))
        assert km.n == 5
        assert km.censored == 2

    def test_survival_at_is_a_right_continuous_step(self):
        km = kaplan_meier(HAND)
        assert km.survival_at(0) == 1.0
        assert km.survival_at(0.999) == 1.0
        assert km.survival_at(1) == pytest.approx(0.8)
        assert km.survival_at(2.5) == pytest.approx(0.8)
        assert km.survival_at(3) == pytest.approx(0.8 * 2 / 3)
        assert km.survival_at(100) == pytest.approx(0.8 * 2 / 3 / 2)

    def test_median_is_first_time_survival_reaches_half(self):
        # S(3) = 0.533 > 0.5, S(4) = 0.267 <= 0.5.
        assert kaplan_meier(HAND).median == 4.0

    def test_median_undefined_when_mostly_censored(self):
        km = kaplan_meier([(1, True), (10, False), (10, False), (10, False)])
        assert km.survival == pytest.approx((0.75,))
        assert km.median is None

    def test_restricted_mean_is_area_under_the_curve(self):
        km = kaplan_meier(HAND)
        # 1*1 + 0.8*2 + 0.5333*1 + 0.2667*1 over [0, 5]
        assert km.restricted_mean(5) == pytest.approx(1 + 1.6 + 0.8 * 2 / 3 + 0.8 / 3)
        assert km.restricted_mean(1) == pytest.approx(1.0)
        assert km.restricted_mean(2) == pytest.approx(1.8)

    def test_censored_at_an_event_time_stays_at_risk(self):
        # Ties: the fire censored at t=3 counts in n at t=3.
        km = kaplan_meier([(3, True), (3, False), (3, True)])
        assert km.at_risk == (3,)
        assert km.events == (2,)
        assert km.survival == pytest.approx((1 / 3,))

    def test_no_events(self):
        km = kaplan_meier([(5, False), (5, False)])
        assert km == SurvivalCurve((), (), (), (), n=2, censored=2)
        assert km.survival_at(10) == 1.0
        assert km.median is None
        assert km.restricted_mean(5) == 5.0
        assert km.points() == [(0.0, 1.0)]

    def test_all_recovered_reaches_zero(self):
        km = kaplan_meier([(2, True), (1, True)])
        assert km.survival == pytest.approx((0.5, 0.0))
        assert km.median == 1.0

    def test_empty(self):
        km = kaplan_meier([])
        assert km.n == 0 and km.times == ()


def _episode(*parts):
    return {"time_to_recovery": {"fires": [{"parts": p} for p in parts]}}


def _parts(ticks, recovered, budget=1000):
    return {"ticks": float(ticks), "recovered": recovered, "budget_ticks": budget}


class TestPooling:
    def test_recovery_data_skips_unscoreable_fires(self):
        episodes = [
            _episode(_parts(100, True), None),
            _episode(_parts(1000, False)),
        ]
        assert recovery_data(episodes) == [(100.0, True), (1000.0, False)]

    def test_pooled_summary(self):
        episodes = [
            _episode(_parts(100, True), _parts(1000, False)),
            _episode(_parts(300, True)),
            _episode(None),
        ]
        pooled = pooled_time_to_recovery(episodes)
        assert pooled["n"] == 3
        assert pooled["censored"] == 1
        assert pooled["tau"] == 1000
        assert pooled["median_ticks"] == 300.0
        # S: 1 on [0,100), 2/3 on [100,300), 1/3 on [300,1000]
        assert pooled["restricted_mean_ticks"] == pytest.approx(
            100 + 200 * 2 / 3 + 700 / 3
        )
        points = pooled["curve"].points()
        assert [t for t, _ in points] == [0.0, 100.0, 300.0]
        assert [s for _, s in points] == pytest.approx([1.0, 2 / 3, 1 / 3])

    def test_pooled_none_without_scoreable_fires(self):
        assert pooled_time_to_recovery([_episode(None), _episode()]) is None

    def test_explicit_tau(self):
        pooled = pooled_time_to_recovery([_episode(_parts(100, True))], tau=50)
        assert pooled["tau"] == 50
        assert pooled["restricted_mean_ticks"] == 50.0
