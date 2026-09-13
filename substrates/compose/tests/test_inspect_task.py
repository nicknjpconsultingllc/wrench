"""Inspect task construction and the scorers over a canned store."""

import math

import pytest
from inspect_ai.util import Store

from wrench_compose.inspect_task import (
    ComposeData,
    compose_sentinel,
    create_compose_task,
    detection_score,
    recovery_score,
    throughput_retained_score,
)
from wrench_compose.kinds import KINDS
from wrench_compose.positions import position_of

from fakes import synthetic_samples


def canned_data(**overrides):
    fire = {
        "tick": 60000,
        "event": "fired",
        "kind": "entity_destruction",
        "seed": 3,
        "affected": [
            {"service": "worker", "container": "wrench-factory-0-worker-1", "x": 40.0, "y": 0.0, "same_type_total": 2}
        ],
        "detail": {},
    }
    x, y = position_of("worker")
    report = {
        "tick": 66000,
        "event": "report_fault",
        "affected": [{"service": "worker", "x": x, "y": y}],
        "detail": {"service": "worker", "cause": "gone", "x": x, "y": y},
    }
    fields = dict(
        samples=synthetic_samples(180000, 60000, 8.0, 4.0),
        ledger_events=[{"tick": 1000, "event": "armed", "kind": "entity_destruction", "seed": 3}, fire, report],
        end_tick=180000,
    )
    fields.update(overrides)
    return ComposeData(store=Store(), **fields)


def test_throughput_retained_score_shape():
    score = throughput_retained_score(canned_data())
    assert score.value == pytest.approx(0.5, abs=0.02)
    assert score.answer == f"{score.value:.3f}"
    meta = score.metadata
    assert meta["scoreable"] and meta["num_fires"] == 1 and meta["item"] == "jobs_done"
    assert meta["pooled_numerator"] / meta["pooled_denominator"] == pytest.approx(score.value, abs=1e-9)
    assert meta["floor_adjusted_scoreable"] and meta["floor_adjusted_num_fires"] == 1
    assert 0.0 <= meta["floor_adjusted_pooled"] <= 0.1  # half the workers lost, nothing rebuilt
    assert meta["fires"][0]["kind"] == "entity_destruction"


def test_recovery_and_detection_scores():
    data = canned_data()
    rec = recovery_score(data)
    assert rec.value == 0.0 and rec.answer == "0/1" and rec.metadata["scoreable_fires"] == 1
    det = detection_score(data)
    assert det.value == 1.0 and det.metadata["precision_strict"] == 1.0 and det.metadata["latencies"] == [6000]
    assert det.answer == "recall=1.00"


def test_full_recovery_scores_one():
    data = canned_data(samples=synthetic_samples(180000, 60000, 8.0, 8.0))
    assert throughput_retained_score(data).value == pytest.approx(1.0, abs=0.02)
    assert recovery_score(data).value == 1.0


def test_unscoreable_without_a_fire():
    data = canned_data(ledger_events=[], samples=synthetic_samples(30000))
    tr = throughput_retained_score(data)
    assert math.isnan(tr.value) and tr.answer == "unscoreable" and tr.metadata["scoreable"] is False
    assert math.isnan(recovery_score(data).value)
    assert detection_score(data).value == 1.0  # vacuous


def test_task_dataset_is_kinds_times_seeds():
    task = create_compose_task("entity_destruction,belt_cut", "1,3", turns=5)
    ids = [s.id for s in task.dataset]
    assert ids == ["entity_destruction_seed1", "entity_destruction_seed3", "belt_cut_seed1", "belt_cut_seed3"]
    assert task.dataset[0].metadata == {
        "kind": "entity_destruction",
        "seed": 1,
        "turns": 5,
        "turn_period_s": 6.0,
        "substrate": "compose",
    }
    assert task.name == "compose_sentinel"
    assert [s.__name__ if hasattr(s, "__name__") else str(s) for s in task.scorer]  # three scorers attached
    assert len(task.scorer) == 3
    assert create_compose_task("belt_cut").name == "belt_cut"
    assert len(compose_sentinel().dataset) == len(KINDS)
    with pytest.raises(ValueError):
        create_compose_task("meteor_strike")


def test_generate_config_per_model():
    from wrench_compose.inspect_task import MAX_TOKENS, REASONING_TOKENS, generate_config

    assert MAX_TOKENS >= 4096
    for name in ("openai/gpt-5-mini", "gpt-5.1", "o3-mini", "openai/o4-mini"):
        cfg = generate_config(name)
        assert cfg.reasoning_effort == "low" and cfg.reasoning_tokens is None, name
    for name in ("anthropic/claude-sonnet-4.5", "claude-opus-4.1", "google/gemini-2.5-pro", "gemini-2.5-flash"):
        cfg = generate_config(name)
        assert cfg.reasoning_tokens == REASONING_TOKENS and cfg.reasoning_effort is None, name
    for name in ("model", "operator", "gpt-4.1-mini", "deepseek/deepseek-chat"):
        cfg = generate_config(name)
        assert cfg.reasoning_effort is None and cfg.reasoning_tokens is None, name
    cfg = generate_config("anthropic/claude-sonnet-4.5")
    assert cfg.max_tokens == MAX_TOKENS > cfg.reasoning_tokens and cfg.max_retries == 5 and cfg.timeout == 180
