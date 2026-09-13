"""Build the WRENCH compose comparison table: N models x fault kinds x seeds.

One Inspect episode per (model, kind, seed) through
``wrench_compose.inspect_task`` (``inspect_ai.eval_set``, so an interrupted
run resumes with ``--resume``), then a results table (JSON + markdown) under
``<outdir>/<timestamp>/``. The Factorio substrate's ``scripts/run_table.py``
is the same script over its own task; the two share the aggregation rules.

Aggregation is the pooled-ratio rule (``wrench_core.scoring``): every
per-(model, kind) metric is a sum of raw numerators over a sum of raw
denominators across seeds and fires, never a mean of per-episode ratios.
Time-to-recovery is pooled through ``wrench_core.survival``: the per-fire
(ticks, recovered) pairs, right-censored at the window, go through
Kaplan-Meier and the table shows the KM median per (model, kind).

An episode counts as an error row when ``sample.error`` is set OR the
``ComposeData:error`` store field is non-empty: the solver's own
try/except returns normally after an infrastructure failure (a slot that
never came up, a probe that never fired), so ``sample.error`` alone would
report such an episode as a genuine 0-fire success.

Usage:
    python scripts/run_table.py --models openrouter/anthropic/claude-sonnet-4.5,openrouter/openai/gpt-5-mini \\
        --seeds 1,3 [--kinds entity_destruction,belt_cut] [--turns 30]

Use ``--models mockllm/model`` for a plumbing check without API keys and
``--models compose-scripted/operator`` for the scripted repair policy.
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wrench_core.metrics import episode_metrics
from wrench_core.scoring import winsorize_tr
from wrench_core.survival import pooled_time_to_recovery

from wrench_compose.kinds import KINDS, parse_kinds, parse_seeds
from wrench_compose.report import ITEM
from wrench_compose.scoring import COMPOSE_CONFIG

TR_SCORER = "throughput_retained"
RECOVERY_SCORER = "recovery"
DETECTION_SCORER = "detection"
STORE_PREFIX = "ComposeData:"


def _num(value):
    """NaN-safe float -> float | None (NaN is 'not scoreable')."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _fmt(value, digits=3):
    return "-" if value is None else f"{value:.{digits}f}"


def _score_meta(sample, scorer_name):
    score = (sample.scores or {}).get(scorer_name)
    if score is None:
        return None, {}
    return _num(score.value), (score.metadata or {})


def _store(sample) -> dict:
    store = getattr(sample, "store", None) or {}
    return store if hasattr(store, "get") else {}


def episode_error(sample):
    """``sample.error`` or the store's ``ComposeData:error``, whichever is set."""
    store_error = _store(sample).get(STORE_PREFIX + "error") or None
    if sample.error:
        return str(getattr(sample.error, "message", sample.error))
    return store_error


def ttr_record(sample):
    """Re-run ``episode_metrics`` over the store so the per-fire survival
    data (which no scorer's metadata carries) can be pooled."""
    store = _store(sample)
    samples = store.get(STORE_PREFIX + "samples") or []
    ledger = store.get(STORE_PREFIX + "ledger_events") or []
    end_tick = int(store.get(STORE_PREFIX + "end_tick") or 0)
    if not samples:
        return None
    return episode_metrics(
        samples, ledger, store.get(STORE_PREFIX + "quota_item") or ITEM, end_tick, config=COMPOSE_CONFIG
    )


def collect_episode_rows(logs):
    """One row per (model, kind, seed) episode from the eval logs."""
    from inspect_ai.log import read_eval_log

    rows = []
    for log in logs:
        if log.samples is None:
            log = read_eval_log(log.location)
        model = str(log.eval.model)
        task_name = log.eval.task
        if log.status != "success":
            rows.append(
                {
                    "model": model,
                    "kind": task_name,
                    "seed": None,
                    "status": log.status,
                    "error": str(log.error.message) if log.error else None,
                }
            )
            continue
        for sample in log.samples or []:
            meta = sample.metadata or {}
            error = episode_error(sample)
            tr_value, tr_meta = _score_meta(sample, TR_SCORER)
            rec_value, rec_meta = _score_meta(sample, RECOVERY_SCORER)
            det_value, det_meta = _score_meta(sample, DETECTION_SCORER)
            latencies = det_meta.get("latencies") or []
            ep_tr_num = tr_meta.get("pooled_numerator", 0.0)
            ep_tr_den = tr_meta.get("pooled_denominator", 0.0)
            ep_floor_num = tr_meta.get("floor_adjusted_pooled_numerator", 0.0)
            ep_floor_den = tr_meta.get("floor_adjusted_pooled_denominator", 0.0)
            rows.append(
                {
                    "model": model,
                    "kind": meta.get("kind", task_name),
                    "seed": meta.get("seed"),
                    "status": "error" if error else "success",
                    "error": error,
                    "fires": tr_meta.get("num_fires", 0),
                    "throughput_retained": tr_value,
                    "throughput_retained_raw": (ep_tr_num / ep_tr_den if ep_tr_den > 0 else None),
                    "tr_numerator": ep_tr_num,
                    "tr_denominator": ep_tr_den,
                    "throughput_retained_floor_adj": (
                        winsorize_tr(ep_floor_num / ep_floor_den) if ep_floor_den > 0 else None
                    ),
                    "tr_floor_numerator": ep_floor_num,
                    "tr_floor_denominator": ep_floor_den,
                    "recovery_rate": rec_value,
                    "recovered": rec_meta.get("recovered", 0),
                    "scoreable_fires": rec_meta.get("scoreable_fires", 0),
                    "detection_recall": _num(det_meta.get("recall")),
                    "detection_precision": _num(det_meta.get("precision")),
                    "detection_precision_strict": _num(det_meta.get("precision_strict")),
                    "detection_latencies": latencies,
                    "matched_reports": det_meta.get("matched_reports", 0),
                    "matched_reports_strict": det_meta.get("matched_reports_strict", 0),
                    "num_reports": det_meta.get("num_reports", 0),
                    "matched_fires": det_meta.get("matched_fires", 0),
                    "num_fires": det_meta.get("num_fires", 0),
                    "steps_completed": _store(sample).get(STORE_PREFIX + "steps_completed"),
                    "ttr": ttr_record(sample) if not error else None,
                }
            )
    return rows


def aggregate_rows(rows):
    """Pooled per-(model, kind) aggregates: sum numerators / sum denominators,
    plus the Kaplan-Meier time-to-recovery over the group's fires."""
    groups = defaultdict(list)
    for row in rows:
        if row.get("seed") is None and row.get("status") != "success":
            continue  # whole-log failure; surfaced in the episode table
        groups[(row["model"], row["kind"])].append(row)

    aggregates = []
    for (model, kind), episodes in sorted(groups.items()):
        ok = [e for e in episodes if e["status"] == "success"]
        tr_num = sum(e["tr_numerator"] for e in ok)
        tr_den = sum(e["tr_denominator"] for e in ok)
        tr_floor_num = sum(e["tr_floor_numerator"] for e in ok)
        tr_floor_den = sum(e["tr_floor_denominator"] for e in ok)
        recovered = sum(e["recovered"] for e in ok)
        scoreable = sum(e["scoreable_fires"] for e in ok)
        matched_reports = sum(e["matched_reports"] for e in ok)
        matched_strict = sum(e["matched_reports_strict"] for e in ok)
        num_reports = sum(e["num_reports"] for e in ok)
        matched_fires = sum(e["matched_fires"] for e in ok)
        num_fires = sum(e["num_fires"] for e in ok)
        latencies = [lat for e in ok for lat in e["detection_latencies"]]
        ttr = pooled_time_to_recovery([e["ttr"] for e in ok if e.get("ttr")])
        aggregates.append(
            {
                "model": model,
                "kind": kind,
                "episodes": len(episodes),
                "episodes_ok": len(ok),
                "fires": num_fires,
                "throughput_retained": (winsorize_tr(tr_num / tr_den) if tr_den > 0 else None),
                "throughput_retained_raw": (tr_num / tr_den if tr_den > 0 else None),
                "tr_numerator": tr_num,
                "tr_denominator": tr_den,
                "throughput_retained_floor_adj": (
                    winsorize_tr(tr_floor_num / tr_floor_den) if tr_floor_den > 0 else None
                ),
                "tr_floor_numerator": tr_floor_num,
                "tr_floor_denominator": tr_floor_den,
                "recovery_rate": recovered / scoreable if scoreable else None,
                "recovered": recovered,
                "scoreable_fires": scoreable,
                "detection_recall": (matched_fires / num_fires if num_fires else 1.0),
                "detection_precision": (matched_reports / num_reports if num_reports else 1.0),
                "detection_precision_strict": (matched_strict / num_reports if num_reports else 1.0),
                "mean_detection_latency_ticks": (sum(latencies) / len(latencies) if latencies else None),
                "ttr_km_median_ticks": ttr["median_ticks"] if ttr else None,
                "ttr_restricted_mean_ticks": ttr["restricted_mean_ticks"] if ttr else None,
                "ttr_n": ttr["n"] if ttr else 0,
                "ttr_censored": ttr["censored"] if ttr else 0,
            }
        )
    return aggregates


def write_markdown(path: Path, aggregates, rows, args):
    lines = [
        "# WRENCH compose comparison table",
        "",
        f"- models: {', '.join(args.models)}",
        f"- kinds: {', '.join(args.kind_list)}",
        f"- seeds: {', '.join(str(s) for s in args.seed_list)}",
        f"- turns per episode: {args.turns}",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Aggregates use the pooled-ratio rule: sum of raw numerators over sum",
        "of raw denominators across seeds/fires (never mean-of-ratios).",
        "`-` = not scoreable (no fires with a valid frozen baseline).",
        "TR is winsorized to [-0.5, 1.5] (wrench_core.scoring) and is the",
        "headline metric; TR (raw) is the same pooled ratio before the clamp.",
        "TR (floor-adj) subtracts the passive-redundancy floor (entity_destruction",
        "fires carrying same_type_total) from numerator and denominator.",
        "TTR median is the Kaplan-Meier median time to sustained recovery in ms",
        "over the group's fires (censored fires stay at risk until the window",
        "end; `-` when fewer than half recovered).",
        "",
        "## Per-(model, kind) aggregates",
        "",
        "| Model | Kind | Episodes | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | TTR median (ms) "
        "| Det. recall | Det. precision (strict) | Det. precision (loose) | Det. latency (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in aggregates:
        lines.append(
            f"| {a['model']} | {a['kind']} | {a['episodes_ok']}/{a['episodes']} "
            f"| {a['fires']} | {_fmt(a['throughput_retained'])} "
            f"| {_fmt(a['throughput_retained_raw'])} "
            f"| {_fmt(a['throughput_retained_floor_adj'])} "
            f"| {_fmt(a['recovery_rate'], 2)} "
            f"| {_fmt(a['ttr_km_median_ticks'], 0)} "
            f"| {_fmt(a['detection_recall'], 2)} "
            f"| {_fmt(a['detection_precision_strict'], 2)} "
            f"| {_fmt(a['detection_precision'], 2)} "
            f"| {_fmt(a['mean_detection_latency_ticks'], 0)} |"
        )
    lines += [
        "",
        "## Per-episode results",
        "",
        "| Model | Kind | Seed | Status | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall | Error |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        err = (r.get("error") or "").splitlines()[0][:80] if r.get("error") else ""
        lines.append(
            f"| {r['model']} | {r['kind']} | {r.get('seed', '-')} "
            f"| {r['status']} | {r.get('fires', '-')} "
            f"| {_fmt(r.get('throughput_retained'))} "
            f"| {_fmt(r.get('throughput_retained_raw'))} "
            f"| {_fmt(r.get('throughput_retained_floor_adj'))} "
            f"| {_fmt(r.get('recovery_rate'), 2)} "
            f"| {_fmt(r.get('detection_recall'), 2)} | {err} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Run the WRENCH compose model-comparison table via Inspect.")
    ap.add_argument("--models", required=True, help="Comma-separated Inspect model names")
    ap.add_argument("--seeds", default="1", help="Comma-separated seeds per (model, kind), e.g. 1,3")
    ap.add_argument("--kinds", default=None, help=f"Comma-separated subset of {KINDS} (default: all)")
    ap.add_argument("--turns", type=int, default=30, help="Turn budget per episode")
    ap.add_argument("--turn-period", type=float, default=6.0, help="Minimum seconds between two commands")
    ap.add_argument("--outdir", default="table_runs")
    ap.add_argument(
        "--resume",
        default=None,
        help="An existing run dir to resume into (eval_set skips completed (model, kind, seed) logs there).",
    )
    ap.add_argument("--max-connections", type=int, default=4, help="Max concurrent model calls")
    ap.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Max concurrent episodes (default: WRENCH_COMPOSE_SLOTS; more than that just waits for a slot)",
    )
    args = ap.parse_args()

    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        args.kind_list = parse_kinds(args.kinds)
        args.seed_list = parse_seeds(args.seeds)
    except ValueError as e:
        ap.error(str(e))

    import os

    from inspect_ai import eval_set

    from wrench_compose.inspect_task import create_compose_task
    from wrench_compose.slots import configured_slots

    if args.resume:
        run_dir = Path(args.resume)
        if not run_dir.is_dir():
            ap.error(f"--resume path does not exist or is not a directory: {run_dir}")
        resuming = True
    else:
        run_dir = Path(args.outdir) / time.strftime("%Y%m%dT%H%M%S")
        resuming = False
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("WRENCH_COMPOSE_SLOTS", "1")

    tasks = [
        create_compose_task(
            kinds=kind,
            seeds=args.seed_list,
            turns=args.turns,
            turn_period_s=args.turn_period,
            out_root=str(run_dir / "episodes"),
            name=kind,
        )
        for kind in args.kind_list
    ]
    print(
        f"{'Resuming' if resuming else 'Running'} {len(args.models)} model(s) x {len(args.kind_list)} kind(s) "
        f"x {len(args.seed_list)} seed(s) on {configured_slots()} slot(s) -> {run_dir}"
    )
    eval_kwargs = dict(
        tasks=tasks,
        model=args.models,
        log_dir=str(run_dir / "logs"),
        max_connections=args.max_connections,
        max_samples=args.max_samples or configured_slots(),
        fail_on_error=False,
    )
    success, logs = eval_set(**eval_kwargs)
    if not success:
        print("WARNING: some evals did not complete successfully; partial results follow (re-run with --resume).")

    rows = collect_episode_rows(logs)
    aggregates = aggregate_rows(rows)
    results = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "models": args.models,
        "kinds": args.kind_list,
        "seeds": args.seed_list,
        "turns": args.turns,
        "log_dir": str(run_dir / "logs"),
        "pooling": "sum of raw numerators / sum of raw denominators across seeds and fires (wrench_core.scoring); "
        "time-to-recovery via Kaplan-Meier (wrench_core.survival)",
        "episodes": [{k: v for k, v in r.items() if k != "ttr"} for r in rows],
        "aggregates": aggregates,
    }
    json_path = run_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    md_path = run_dir / "results.md"
    write_markdown(md_path, aggregates, rows, args)

    print(f"\nWrote {json_path}\nWrote {md_path}\n")
    print(md_path.read_text())
    for a in aggregates:
        print(
            f"TTR KM median {a['model']} {a['kind']}: {_fmt(a['ttr_km_median_ticks'], 0)} ms "
            f"(n={a['ttr_n']}, censored={a['ttr_censored']}, restricted mean {_fmt(a['ttr_restricted_mean_ticks'], 0)} ms)"
        )
    errors = [r for r in rows if r["status"] != "success"]
    if errors:
        print(f"\n{len(errors)} error row(s):")
        for r in errors:
            print(f"  {r['model']} {r['kind']} seed={r.get('seed')}: {(r.get('error') or '')[:200]}")


if __name__ == "__main__":
    main()
