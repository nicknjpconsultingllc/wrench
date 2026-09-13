"""Arming scheduler on canned sample series (no Docker)."""

from wrench_compose.arming import Scheduler, trailing_rate

ITEM = "jobs_done"


def series(rate_per_s, seconds, step_ms=500, start_count=0):
    """Cumulative samples at a constant rate."""
    out = []
    n = int(seconds * 1000 / step_ms)
    for i in range(n + 1):
        t = i * step_ms
        out.append({"tick": t, "counts": {ITEM: start_count + int(rate_per_s * t / 1000)}})
    return out


def test_trailing_rate_needs_full_window():
    s = series(8, 30)
    assert trailing_rate(s, ITEM, 60000) is None
    s = series(8, 61)
    assert abs(trailing_rate(s, ITEM, 60000) - 480) < 1


def test_arms_after_two_consecutive_windows_at_quota_and_fires():
    sched = Scheduler()
    fired = []

    def fire(spec):
        fired.append(spec.kind)
        return [{"service": "worker", "container": "w-1", "same_type_total": 2}], None, False

    sched.arm("entity_destruction", seed=3, quota_per_min=400, consecutive_windows=2, window_ms=60000)
    events = []
    for s in series(8, 70):
        sched.add_sample(s["tick"], s["counts"])
        events += sched.step(s["tick"], fire)
    kinds = [e["event"] for e in events]
    assert kinds == ["armed", "fired"], kinds
    armed, fired_ev = events
    # rate first satisfies the 60 s window at t=60000 (streak 1), arms at the next sample
    assert armed["tick"] == 60500
    assert fired_ev["tick"] == 60500
    assert fired_ev["affected"][0]["same_type_total"] == 2
    assert fired_ev["kind"] == "entity_destruction" and fired_ev["seed"] == 3
    assert fired == ["entity_destruction"]
    assert sched.specs == {}


def test_below_quota_never_arms_and_streak_resets():
    sched = Scheduler()
    sched.arm("belt_cut", seed=1, quota_per_min=400, consecutive_windows=2)
    for s in series(5, 120):  # 300/min < 400
        sched.add_sample(s["tick"], s["counts"])
        assert sched.step(s["tick"], lambda spec: ([], None, False)) == []
    assert sched.specs[1].state == "waiting"
    assert sched.specs[1].streak == 0


def test_quota_fraction_and_delay():
    sched = Scheduler()
    sched.arm("belt_cut", seed=1, quota_per_min=400, quota_fraction=0.5, consecutive_windows=2, delay_ms=5000)
    events = []
    for s in series(5, 70):  # 300/min >= 200
        sched.add_sample(s["tick"], s["counts"])
        events += sched.step(s["tick"], lambda spec: ([{"service": "worker"}], None, False))
    assert [e["event"] for e in events] == ["armed", "fired"]
    assert events[1]["tick"] - events[0]["tick"] == 5000


def test_not_applicable_and_failed_are_distinct():
    for design_avoided, expected in ((True, "not_applicable"), (False, "failed")):
        sched = Scheduler()
        sched.arm("belt_cut", seed=1, quota_per_min=1)
        events = []

        def fire(spec, avoided=design_avoided):
            return None, "no belts", avoided

        for s in series(8, 62):
            sched.add_sample(s["tick"], s["counts"])
            events += sched.step(s["tick"], fire)
        assert [e["event"] for e in events] == ["armed", expected]
        assert events[1]["detail"]["error"] == "no belts"


def test_chained_spec_waits_for_predecessor_then_reproves_on_post_damage_samples():
    sched = Scheduler()
    first = sched.arm("entity_destruction", seed=1, quota_per_min=400)
    sched.arm("belt_cut", seed=1, quota_per_min=400, after_id=first)
    events = []
    t = 0
    count = 0
    # 70 s at quota -> first fires; then 30 s of damage (2/s), then 70 s back at quota
    for rate, secs in ((8, 70), (2, 30), (8, 70)):
        for _ in range(int(secs * 2)):
            t += 500
            count += rate / 2
            sched.add_sample(t, {ITEM: int(count)})
            events += sched.step(t, lambda spec: ([{"service": "x"}], None, False))
    seq = [(e["event"], e["kind"]) for e in events]
    assert seq == [
        ("armed", "entity_destruction"),
        ("fired", "entity_destruction"),
        ("armed", "belt_cut"),
        ("fired", "belt_cut"),
    ]
    first_fire = events[1]["tick"]
    second_fire = events[3]["tick"]
    # the chained spec only counts samples after the first fire (min_tick =
    # predecessor's resolve tick, as in server.lua), so it needs a full
    # post-fire window before it can even evaluate, and it can only meet
    # quota once most of that window is post-damage production
    assert second_fire >= first_fire + 60000
    assert second_fire > first_fire + 30000 + 50000
