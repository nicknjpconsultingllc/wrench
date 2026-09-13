"""Score a run directory (samples.jsonl + ledger.jsonl) with ``wrench_core``."""

import json
from pathlib import Path

from wrench_compose.ledger import read_jsonl
from wrench_compose.scoring import COMPOSE_CONFIG
from wrench_core import scoring

ITEM = "jobs_done"


def load_samples(run_dir: Path) -> list[dict]:
    with (run_dir / "samples.jsonl").open() as f:
        return [json.loads(line) for line in f if line.strip()]


def score_run(run_dir: Path, window_ms: int) -> dict:
    samples = load_samples(run_dir)
    ledger = [e.model_dump() for e in read_jsonl(run_dir / "ledger.jsonl")]
    fires = [e for e in ledger if e["event"] == "fired"]
    out = {"fires": [], "detection": None, "n_samples": len(samples)}
    for fire in fires:
        ft = fire["tick"]
        baseline = scoring.frozen_baseline(samples, ITEM, ft, config=COMPOSE_CONFIG)
        parts = scoring.throughput_retained_parts(samples, ITEM, ft, window_ms, config=COMPOSE_CONFIG)
        floor_parts = scoring.floor_adjusted_throughput_retained_parts(
            samples, ITEM, ft, window_ms, fire, config=COMPOSE_CONFIG
        )
        ttr = scoring.time_to_recovery_parts(samples, ITEM, ft, window_ms, config=COMPOSE_CONFIG)
        entry = {
            "kind": fire["kind"],
            "fire_tick": ft,
            "affected": fire["affected"],
            "baseline_per_min": baseline,
            "tr": scoring.winsorize_tr(parts[0] / parts[1]) if parts else None,
            "tr_raw": parts[0] / parts[1] if parts else None,
            "tr_floor_adj": scoring.winsorize_tr(floor_parts[0] / floor_parts[1])
            if floor_parts and floor_parts[1] > 0
            else None,
            "actual_jobs": parts[0] if parts else None,
            "expected_jobs": parts[1] if parts else None,
            "recovered": scoring.recovery_at(samples, ITEM, ft, window_ms, config=COMPOSE_CONFIG),
            "ttr_ms": ttr["ticks"] if ttr else None,
        }
        out["fires"].append(entry)
    out["detection"] = scoring.detection_metrics(ledger, fires, config=COMPOSE_CONFIG) if fires else None
    reports = [e for e in ledger if e["event"] == "report_fault"]
    out["n_reports"] = len(reports)
    out["not_applicable"] = [e for e in ledger if e["event"] == "not_applicable"]
    out["failed"] = [e for e in ledger if e["event"] == "failed"]
    return out


def one_line(name: str, s: dict) -> str:
    if not s["fires"]:
        why = s["not_applicable"] or s["failed"]
        return f"{name:<44} no fire ({why[0]['detail'].get('error') if why else 'never armed'})"
    f = s["fires"][0]
    det = s["detection"] or {}
    lat = det.get("latencies") or []

    def fmt(v):
        return "None" if v is None else f"{v:.3f}"

    return (
        f"{name:<44} base={f['baseline_per_min']:.1f}/min fire@{f['fire_tick'] / 1000:.1f}s "
        f"TR={fmt(f['tr'])} raw={fmt(f['tr_raw'])} floor-adj={fmt(f['tr_floor_adj'])} "
        f"recovered={f['recovered']} TTR={f['ttr_ms']}ms "
        f"det: P={det.get('precision_strict')} R={det.get('recall')} lat={lat[0] if lat else None}ms"
    )
