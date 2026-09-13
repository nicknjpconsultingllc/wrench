"""run_table's row collection (error detection through the store field),
pooled-ratio aggregation and the Kaplan-Meier time-to-recovery pooling,
over fake eval logs."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from wrench_compose.positions import position_of

from fakes import synthetic_samples

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_table.py"


def _load():
    if "run_table" in sys.modules:
        return sys.modules["run_table"]
    spec = importlib.util.spec_from_file_location("run_table", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_table"] = module
    spec.loader.exec_module(module)
    return module


rt = _load()


def score(value, **meta):
    return SimpleNamespace(value=value, metadata=meta)


def fake_sample(
    kind,
    seed,
    *,
    error=None,
    store_error="",
    tr=(300.0, 600.0),
    floor=(0.0, 0.0),
    recovered=0,
    reports=(1, 1, 1),
    ttr_after=4.0,
):
    """An EvalSample-shaped object; the store carries samples/ledger so the
    survival pooling can recompute episode_metrics from it."""
    fire = {
        "tick": 60000,
        "event": "fired",
        "kind": kind,
        "seed": seed,
        "affected": [{"service": "worker", "x": 40.0, "y": 0.0}],
    }
    x, y = position_of("worker")
    ledger = [
        fire,
        {
            "tick": 63000,
            "event": "report_fault",
            "affected": [{"service": "worker", "x": x, "y": y}],
            "detail": {"service": "worker", "x": x, "y": y},
        },
    ]
    return SimpleNamespace(
        id=f"{kind}_seed{seed}",
        metadata={"kind": kind, "seed": seed},
        error=error,
        store={
            "ComposeData:error": store_error,
            "ComposeData:samples": synthetic_samples(180000, 60000, 8.0, ttr_after),
            "ComposeData:ledger_events": ledger,
            "ComposeData:end_tick": 180000,
            "ComposeData:steps_completed": 7,
        },
        scores={
            "throughput_retained": score(
                tr[0] / tr[1],
                num_fires=1,
                pooled_numerator=tr[0],
                pooled_denominator=tr[1],
                floor_adjusted_pooled_numerator=floor[0],
                floor_adjusted_pooled_denominator=floor[1],
            ),
            "recovery": score(float(recovered), recovered=recovered, scoreable_fires=1),
            "detection": score(
                1.0,
                recall=1.0,
                precision=1.0,
                precision_strict=1.0,
                latencies=[3000],
                matched_reports=reports[0],
                matched_reports_strict=reports[1],
                num_reports=reports[2],
                matched_fires=1,
                num_fires=1,
            ),
        },
    )


def fake_log(model, task, samples, status="success"):
    return SimpleNamespace(
        samples=samples,
        location="",
        status=status,
        eval=SimpleNamespace(model=model, task=task),
        error=None,
    )


def test_error_rows_come_from_sample_error_or_the_store():
    logs = [
        fake_log(
            "m",
            "belt_cut",
            [
                fake_sample("belt_cut", 1),
                fake_sample("belt_cut", 2, store_error="WRENCH compose solver error: slot never came up"),
                fake_sample("belt_cut", 3, error=SimpleNamespace(message="sample blew up")),
            ],
        )
    ]
    rows = rt.collect_episode_rows(logs)
    assert [r["status"] for r in rows] == ["success", "error", "error"]
    assert rows[1]["error"].startswith("WRENCH compose solver error")
    assert rows[2]["error"] == "sample blew up"
    assert rows[0]["ttr"] is not None and rows[1]["ttr"] is None
    assert rows[0]["steps_completed"] == 7 and rows[0]["throughput_retained"] == pytest.approx(0.5)


def test_pooled_ratio_not_mean_of_ratios():
    # Episode A: 100/200 (0.5); episode B: 900/1000 (0.9). Mean of ratios 0.7,
    # pooled 1000/1200 = 0.833.
    logs = [
        fake_log(
            "m",
            "entity_destruction",
            [
                fake_sample("entity_destruction", 1, tr=(100.0, 200.0), recovered=0, floor=(50.0, 100.0)),
                fake_sample(
                    "entity_destruction", 3, tr=(900.0, 1000.0), recovered=1, floor=(800.0, 900.0), reports=(1, 0, 2)
                ),
                fake_sample("entity_destruction", 5, store_error="broken"),
            ],
        )
    ]
    agg = rt.aggregate_rows(rt.collect_episode_rows(logs))
    assert len(agg) == 1
    a = agg[0]
    assert a["episodes"] == 3 and a["episodes_ok"] == 2 and a["fires"] == 2
    assert a["throughput_retained"] == pytest.approx(1000 / 1200)
    assert a["throughput_retained_floor_adj"] == pytest.approx(850 / 1000)
    assert a["recovery_rate"] == 0.5
    assert a["detection_precision"] == pytest.approx(2 / 3) and a["detection_precision_strict"] == pytest.approx(1 / 3)
    assert a["detection_recall"] == 1.0 and a["mean_detection_latency_ticks"] == 3000


def test_km_median_pooled_per_model_kind():
    # Two fires that recover (rate back at 8/s from tick 60 s: the trailing
    # 30 s window crosses 0.9x baseline ~27 s after the fire) and one that never does.
    logs = [
        fake_log(
            "m",
            "belt_cut",
            [
                fake_sample("belt_cut", 1, ttr_after=8.0),
                fake_sample("belt_cut", 2, ttr_after=8.0),
                fake_sample("belt_cut", 3, ttr_after=4.0),
            ],
        ),
        fake_log("other", "belt_cut", [fake_sample("belt_cut", 1, ttr_after=4.0)]),
    ]
    agg = {(a["model"], a["kind"]): a for a in rt.aggregate_rows(rt.collect_episode_rows(logs))}
    m = agg[("m", "belt_cut")]
    assert m["ttr_n"] == 3 and m["ttr_censored"] == 1
    assert m["ttr_km_median_ticks"] is not None and 20000 <= m["ttr_km_median_ticks"] <= 40000
    other = agg[("other", "belt_cut")]
    assert other["ttr_n"] == 1 and other["ttr_censored"] == 1 and other["ttr_km_median_ticks"] is None


def test_whole_log_failure_is_surfaced_not_aggregated():
    log = fake_log("m", "adaptive_strike", None, status="error")
    log.error = SimpleNamespace(message="eval crashed")
    log.samples = []
    rows = rt.collect_episode_rows([log])
    assert rows == [{"model": "m", "kind": "adaptive_strike", "seed": None, "status": "error", "error": "eval crashed"}]
    assert rt.aggregate_rows(rows) == []


def test_markdown_table(tmp_path):
    logs = [fake_log("m", "belt_cut", [fake_sample("belt_cut", 1), fake_sample("belt_cut", 3, store_error="x")])]
    rows = rt.collect_episode_rows(logs)
    args = SimpleNamespace(models=["m"], kind_list=["belt_cut"], seed_list=[1, 3], turns=30)
    path = tmp_path / "results.md"
    rt.write_markdown(path, rt.aggregate_rows(rows), rows, args)
    text = path.read_text()
    assert "| m | belt_cut | 1/2 | 1 | 0.500 |" in text
    assert "| m | belt_cut | 3 | error |" in text and "| x |" in text


def test_main_passes_retry_on_error_to_eval_set(tmp_path, monkeypatch):
    import inspect_ai

    calls = {}

    def fake_eval_set(**kwargs):
        calls.update(kwargs)
        return True, []

    monkeypatch.setattr(inspect_ai, "eval_set", fake_eval_set)
    monkeypatch.setattr(
        sys, "argv", ["run_table.py", "--models", "mockllm/model", "--kinds", "belt_cut", "--outdir", str(tmp_path)]
    )
    rt.main()
    assert calls["retry_on_error"] == rt.RETRY_ON_ERROR == 2 and calls["fail_on_error"] is False


def test_pooled_recall_is_gated_by_pooled_precision():
    # Each episode: 1 matched report out of 4 (precision 0.25 < 0.5), fire matched.
    logs = [
        fake_log(
            "m",
            "belt_cut",
            [fake_sample("belt_cut", 1, reports=(1, 1, 4)), fake_sample("belt_cut", 3, reports=(1, 1, 4))],
        ),
        fake_log("precise", "belt_cut", [fake_sample("belt_cut", 1, reports=(1, 1, 2))]),
    ]
    agg = {a["model"]: a for a in rt.aggregate_rows(rt.collect_episode_rows(logs))}
    assert agg["m"]["detection_precision"] == pytest.approx(0.25)
    assert agg["m"]["detection_recall"] == 0.0  # 2/2 fires matched, but the spam gate holds
    assert agg["precise"]["detection_precision"] == 0.5 and agg["precise"]["detection_recall"] == 1.0
    assert rt.pooled_recall(2, 2, 0.49) == 0.0 and rt.pooled_recall(1, 2, 0.5) == 0.5


def test_zero_fire_group_renders_recall_as_dash(tmp_path):
    logs = [fake_log("m", "belt_cut", [fake_sample("belt_cut", 1, store_error="never armed")])]
    rows = rt.collect_episode_rows(logs)
    (agg,) = rt.aggregate_rows(rows)
    assert agg["fires"] == 0 and agg["detection_recall"] is None
    path = tmp_path / "results.md"
    rt.write_markdown(path, [agg], rows, SimpleNamespace(models=["m"], kind_list=["belt_cut"], seed_list=[1], turns=5))
    line = next(ln for ln in path.read_text().splitlines() if ln.startswith("| m | belt_cut | 0/1 |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[9] == "-"  # Det. recall
