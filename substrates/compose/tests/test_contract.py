"""Sample and ledger shapes against the fork's scorers (loaded via the shim)."""

import json
from pathlib import Path

import pytest

from wrench_compose.ledger import LedgerEntry, check_sample, read_jsonl, write_jsonl
from wrench_compose.scoring import COMPOSE_CONFIG
from wrench_core import scoring

ITEM = "jobs_done"


def constant(rate_per_s, t0, t1, c0=0, step=500):
    return [{"tick": t, "counts": {ITEM: c0 + int(rate_per_s * (t - t0) / 1000)}} for t in range(t0, t1 + 1, step)]


def test_sample_shape_matches_contract():
    for s in constant(8, 0, 5000):
        check_sample(s)
    with pytest.raises(AssertionError):
        check_sample({"tick": 1.5, "counts": {}})
    with pytest.raises(AssertionError):
        check_sample({"tick": 1, "counts": [1]})


def test_ledger_roundtrip_matches_fork_shape(tmp_path: Path):
    entries = [
        {"tick": 60500, "event": "armed", "kind": "entity_destruction", "seed": 3, "affected": [], "detail": {"id": 1}},
        {
            "tick": 60500,
            "event": "fired",
            "kind": "entity_destruction",
            "seed": 3,
            "affected": [{"service": "worker", "container": "w-1", "x": 40.0, "y": 0.0, "same_type_total": 2}],
            "detail": {"id": 1},
        },
        {
            "tick": 63000,
            "event": "report_fault",
            "affected": [{"service": "worker", "x": 40.0, "y": 0.0}],
            "detail": {"service": "worker", "cause": "gone", "x": 40.0, "y": 0.0},
        },
    ]
    write_jsonl(tmp_path / "ledger.jsonl", entries)
    back = read_jsonl(tmp_path / "ledger.jsonl")
    assert [e.event for e in back] == ["armed", "fired", "report_fault"]
    assert back[1].affected[0]["same_type_total"] == 2
    line = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[2])
    assert set(line) == {"tick", "event", "affected", "detail"}  # exclude_none, like the fork
    with pytest.raises(ValueError):  # pydantic ValidationError, extra="forbid"
        LedgerEntry(tick=1, event="x", bogus=1)


def test_shim_rebases_tick_unit_to_ms():
    assert COMPOSE_CONFIG.ticks_per_minute == 60000
    samples = constant(8, 0, 70000)
    assert abs(scoring.frozen_baseline(samples, ITEM, 60000, config=COMPOSE_CONFIG) - 480) < 1


def test_tr_noop_half_loss_and_oracle_full_on_synthetic_series():
    pre = constant(8, 0, 60000)
    fire = 60000
    # no-op: rate halves for the whole window
    noop = pre + constant(4, fire, fire + 120000, c0=pre[-1]["counts"][ITEM])[1:]
    tr = scoring.throughput_retained(noop, ITEM, fire, 120000, config=COMPOSE_CONFIG)
    assert abs(tr - 0.5) < 0.02
    assert scoring.recovery_at(noop, ITEM, fire, 120000, config=COMPOSE_CONFIG) is False
    # oracle: 3 s at half rate then full rate
    c = pre[-1]["counts"][ITEM]
    oracle = pre + constant(4, fire, fire + 3000, c0=c)[1:]
    c2 = oracle[-1]["counts"][ITEM]
    oracle += constant(8, fire + 3000, fire + 120000, c0=c2)[1:]
    tr = scoring.throughput_retained(oracle, ITEM, fire, 120000, config=COMPOSE_CONFIG)
    assert 0.95 < tr <= 1.0
    assert scoring.recovery_at(oracle, ITEM, fire, 120000, config=COMPOSE_CONFIG) is True
    ttr = scoring.time_to_recovery_parts(oracle, ITEM, fire, 120000, config=COMPOSE_CONFIG)
    assert ttr["recovered"] and 30000 <= ttr["ticks"] <= 36000  # 30 s trailing window + 2 samples


def test_floor_adjusted_tr_uses_same_type_total_from_manifest():
    pre = constant(8, 0, 60000)
    fire = 60000
    noop = pre + constant(4, fire, fire + 120000, c0=pre[-1]["counts"][ITEM])[1:]
    fired = {
        "tick": fire,
        "event": "fired",
        "kind": "entity_destruction",
        "affected": [{"service": "worker", "container": "w-1", "same_type_total": 2}],
    }
    adj = scoring.floor_adjusted_throughput_retained(noop, ITEM, fire, 120000, fired, config=COMPOSE_CONFIG)
    assert abs(adj) < 0.05  # passive redundancy kept half; the no-op earns ~0 credit
    spof = dict(fired, affected=[{"service": "gateway", "container": "g-1", "same_type_total": 1}])
    assert (
        abs(scoring.floor_adjusted_throughput_retained(noop, ITEM, fire, 120000, spof, config=COMPOSE_CONFIG) - 0.5)
        < 0.02
    )


def test_detection_matches_on_service_positions():
    fired = {
        "tick": 60000,
        "event": "fired",
        "kind": "entity_destruction",
        "affected": [{"service": "worker", "container": "w-1", "x": 40.0, "y": 0.0, "same_type_total": 2}],
    }
    right = {"tick": 63000, "event": "report_fault", "detail": {"service": "worker", "x": 40.0, "y": 0.0}}
    wrong = {"tick": 63000, "event": "report_fault", "detail": {"service": "redis", "x": 20.0, "y": 0.0}}
    early = {"tick": 50000, "event": "report_fault", "detail": {"service": "worker", "x": 40.0, "y": 0.0}}
    m = scoring.detection_metrics([fired, right], [fired], config=COMPOSE_CONFIG)
    assert m["precision_strict"] == 1.0 and m["recall"] == 1.0 and m["latencies"] == [3000]
    m = scoring.detection_metrics([fired, wrong], [fired], config=COMPOSE_CONFIG)
    assert m["precision"] == 0.0 and m["recall"] == 0.0
    m = scoring.detection_metrics([fired, early], [fired], config=COMPOSE_CONFIG)
    assert m["recall"] == 0.0
