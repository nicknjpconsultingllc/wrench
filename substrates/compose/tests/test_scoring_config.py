"""The compose ``ScoringConfig``: which kinds carry a redundancy floor."""

import pytest
from wrench_core.metrics import episode_metrics

from wrench_compose.report import ITEM
from wrench_compose.scoring import COMPOSE_CONFIG, REDUNDANCY_KINDS

from fakes import synthetic_samples


def fire(kind, **entry):
    affected = [{"service": "worker", "container": "wrench-factory-0-worker-1", "x": 40.0, "y": 0.0, **entry}]
    return {"tick": 60000, "event": "fired", "kind": kind, "seed": 3, "affected": affected, "detail": {}}


def floor_adj(kind, same_type_total=2):
    metrics = episode_metrics(
        synthetic_samples(180000, 60000, 8.0, 4.0),
        [fire(kind, same_type_total=same_type_total)],
        ITEM,
        180000,
        config=COMPOSE_CONFIG,
    )
    return metrics["scalars"]["throughput_retained_floor_adj"], metrics["scalars"]["throughput_retained"]


def test_both_kill_kinds_carry_the_floor():
    assert REDUNDANCY_KINDS == COMPOSE_CONFIG.redundancy_kinds == {"entity_destruction", "adaptive_strike"}
    for kind in ("entity_destruction", "adaptive_strike"):
        adj, plain = floor_adj(kind)
        assert plain == pytest.approx(0.5, abs=0.02)
        assert adj is not None and adj == pytest.approx(0.0, abs=0.05), kind  # one of two lost, nothing rebuilt


def test_single_point_of_failure_floor_is_plain_tr():
    adj, plain = floor_adj("adaptive_strike", same_type_total=1)
    assert adj == pytest.approx(plain)


@pytest.mark.parametrize("kind", ["belt_cut", "resource_exhaustion", "silent_throttle"])
def test_non_kill_kinds_have_no_floor(kind):
    adj, plain = floor_adj(kind)
    assert adj is None and plain == pytest.approx(0.5, abs=0.02)
