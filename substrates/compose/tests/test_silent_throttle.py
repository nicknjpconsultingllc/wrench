"""silent_throttle unit checks (no Docker): the manifest through
``wrench_core``'s detection scorer, and the floor/bracket logic on canned
throughput series.

The fault lives entirely inside ``postgres`` (an AFTER INSERT trigger that
sleeps on every commit), so exactly one service is right: a report naming
``postgres`` earns credit, and one naming the obviously-slow ``worker`` is a
precision miss. Nothing is destroyed, so the kind carries no redundancy floor.
"""

import pytest
from wrench_core.metrics import episode_metrics
from wrench_core.scoring import detection_metrics

from wrench_compose.kinds import KINDS, STEALTH_KINDS, parse_kinds
from wrench_compose.manifest import SILENT_THROTTLE_SERVICE, silent_throttle_affected
from wrench_compose.positions import position_of
from wrench_compose.report import ITEM
from wrench_compose.scoring import COMPOSE_CONFIG, REDUNDANCY_KINDS

from fakes import synthetic_samples

FIRE_TICK = 60000
END_TICK = 180000


def report(tick, service):
    x, y = position_of(service)
    return {"tick": tick, "event": "report_fault", "detail": {"service": service, "cause": "test", "x": x, "y": y}}


def fire(delay_s=2.0):
    return {
        "tick": FIRE_TICK,
        "event": "fired",
        "kind": "silent_throttle",
        "seed": 1,
        "affected": silent_throttle_affected("wrench-factory-0-postgres-1", delay_s, same_type_total=1),
    }


def test_registered_as_a_stealth_kind():
    assert "silent_throttle" in KINDS
    assert "silent_throttle" in parse_kinds(None)
    assert STEALTH_KINDS == {"silent_throttle"}


def test_manifest_names_only_postgres():
    affected = fire()["affected"]
    assert [e["service"] for e in affected] == [SILENT_THROTTLE_SERVICE]
    entry = affected[0]
    assert entry["mechanism"] == "commit_trigger_delay" and entry["delay_s"] == 2.0
    assert (entry["x"], entry["y"]) == position_of("postgres")


def test_report_postgres_matches_strict():
    f = fire()
    det = detection_metrics([f, report(66000, "postgres")], [f], config=COMPOSE_CONFIG)
    assert det["precision_strict"] == 1.0 and det["precision"] == 1.0 and det["recall"] == 1.0
    assert det["latencies"] == [6000]


@pytest.mark.parametrize("service", ["worker", "redis", "gateway", "netproxy"])
def test_report_on_the_slow_symptom_service_is_a_miss(service):
    f = fire()
    det = detection_metrics([f, report(66000, service)], [f], config=COMPOSE_CONFIG)
    assert det["precision_strict"] == 0.0 and det["precision"] == 0.0 and det["recall"] == 0.0


def test_never_reporting_scores_recall_zero():
    """The status-only detection story in canned form: a fire with no report at
    all leaves recall 0, whereas the container-removal kinds get a free report
    off ``ps``."""
    f = fire()
    det = detection_metrics([f], [f], config=COMPOSE_CONFIG)
    assert det["recall"] == 0.0


def _tr(rate_after):
    """Pooled TR for a canned series that drops to ``rate_after`` jobs/s at the
    fire (baseline 8 jobs/s)."""
    samples = synthetic_samples(END_TICK, FIRE_TICK, rate_before=8.0, rate_after=rate_after)
    scalars = episode_metrics(samples, [fire()], ITEM, END_TICK, config=COMPOSE_CONFIG)["scalars"]
    return scalars["throughput_retained"], scalars["throughput_retained_floor_adj"]


def test_no_floor_and_floor_test_passes_on_canned_collapse():
    # A no-op collapse to ~1 job/s (an 87% loss at 8 jobs/s) clears the <= 0.2 floor.
    tr, adj = _tr(1.0)
    assert tr <= 0.2
    assert adj is None  # not a redundancy kind: floor-adjusted TR stays None
    assert "silent_throttle" not in REDUNDANCY_KINDS


def test_bracket_holds_when_the_delay_is_cleared():
    # An oracle that clears the delay returns to the load-limited baseline.
    tr, _ = _tr(8.0)
    assert tr >= 0.8
