"""One episode's full metric set from raw episode data.

``episode_metrics`` is the single computation every consumer shares (the
Factorio Inspect scorers, ``WrenchEpisode.finalize``, the verifiers rubric,
and the compose substrate), so the paths cannot drift. Inputs are the
substrate contract: ``samples`` (``{"tick", "counts"}`` dicts),
``ledger_events`` (``LedgerEntry.model_dump()`` dicts), the quota item and
the episode's final tick.
"""

from typing import Any, Dict, List, Optional

from wrench_core.scoring import (
    DEFAULT_CONFIG,
    ScoringConfig,
    detection_counts,
    detection_metrics,
    floor_adjusted_throughput_retained_parts,
    frozen_baseline,
    recovery_at,
    throughput_retained_parts,
    time_to_recovery_parts,
    winsorize_tr,
)


def fired_events(ledger_events: List[dict]) -> List[dict]:
    return [e for e in ledger_events if e.get("event") == "fired"]


def tracked_item(samples: List[dict], fallback: str) -> str:
    """Tracked item name; the substrate's sample counts key is authoritative."""
    for sample in reversed(samples):
        counts = sample.get("counts") or {}
        if counts:
            return next(iter(counts))
    return fallback


def _fire_summary(fire: dict) -> dict:
    return {
        "kind": fire.get("kind"),
        "tick": fire.get("tick"),
        "seed": fire.get("seed"),
    }


def episode_metrics(
    samples: List[dict],
    ledger_events: List[dict],
    quota_item: str,
    end_tick: int,
    *,
    config: ScoringConfig = DEFAULT_CONFIG,
) -> Dict[str, Any]:
    """Every post-hoc WRENCH number for one episode, from raw episode data.

    Returns three blocks mirroring the Factorio Inspect scorers
    (``throughput_retained``, ``recovery``, ``detection`` -- each
    ``{"value", "metadata"}``), plus ``time_to_recovery`` (per-fire
    ``(ticks, recovered)`` survival data; see
    ``scoring.time_to_recovery_parts`` for why it is not folded into
    ``recovery``) and a flat ``scalars`` dict for consumers that want one
    number per metric.

    Denominator policy (mirrors ``wrench_core.scoring``): a metric that is
    not scoreable for this episode (no fires, degenerate baseline) has
    ``value None`` and ``metadata["scoreable"] False``. Cross-episode
    aggregation must pool the raw numerators/denominators carried in
    metadata (sum numerators / sum denominators), never average per-episode
    ratios; time-to-recovery pools through ``wrench_core.survival``.
    """
    fires = fired_events(ledger_events)
    item = tracked_item(samples, quota_item)

    # -- Throughput Retained, pooled over fires ----------------------------
    per_fire_tr = []
    num = 0.0
    den = 0.0
    floor_num = 0.0
    floor_den = 0.0
    floor_adjusted_num_fires = 0
    for fire in fires:
        fire_tick = int(fire.get("tick", 0))
        horizon = max(0, end_tick - fire_tick)
        parts = throughput_retained_parts(
            samples, item, fire_tick, horizon, config=config
        )
        entry = _fire_summary(fire)
        entry["horizon_ticks"] = horizon
        entry["baseline_per_min"] = frozen_baseline(
            samples, item, fire_tick, config=config
        )
        if parts is not None:
            actual, expected = parts
            entry["actual"] = actual
            entry["expected"] = expected
            entry["tr"] = winsorize_tr(actual / expected)
            num += actual
            den += expected
        else:
            entry["actual"] = None
            entry["expected"] = None
            entry["tr"] = None

        floor_parts = floor_adjusted_throughput_retained_parts(
            samples, item, fire_tick, horizon, fire, config=config
        )
        if floor_parts is not None:
            floor_actual, floor_expected = floor_parts
            entry["floor_actual"] = floor_actual
            entry["floor_expected"] = floor_expected
            entry["floor_tr"] = winsorize_tr(floor_actual / floor_expected)
            floor_num += floor_actual
            floor_den += floor_expected
            floor_adjusted_num_fires += 1
        else:
            entry["floor_actual"] = None
            entry["floor_expected"] = None
            entry["floor_tr"] = None
        per_fire_tr.append(entry)

    scoreable = den > 0
    pooled: Optional[float] = winsorize_tr(num / den) if scoreable else None
    floor_scoreable = floor_den > 0
    floor_pooled: Optional[float] = (
        winsorize_tr(floor_num / floor_den) if floor_scoreable else None
    )
    throughput_retained = {
        "value": pooled,
        "metadata": {
            "scoreable": scoreable,
            "item": item,
            "num_fires": len(fires),
            "pooled_numerator": num,
            "pooled_denominator": den,
            "floor_adjusted_scoreable": floor_scoreable,
            "floor_adjusted_num_fires": floor_adjusted_num_fires,
            "floor_adjusted_pooled": floor_pooled,
            "floor_adjusted_pooled_numerator": floor_num,
            "floor_adjusted_pooled_denominator": floor_den,
            "fires": per_fire_tr,
        },
    }

    # -- Recovery at budget (budget = remaining episode) ---------------------
    per_fire_recovery = []
    ttr_per_fire = []
    recovered = 0
    scoreable_fires = 0
    for fire in fires:
        fire_tick = int(fire.get("tick", 0))
        budget = max(0, end_tick - fire_tick)
        result = recovery_at(samples, item, fire_tick, budget, config=config)
        entry = _fire_summary(fire)
        entry["budget_ticks"] = budget
        entry["recovered"] = result
        per_fire_recovery.append(entry)
        if result is not None:
            scoreable_fires += 1
            if result:
                recovered += 1
        ttr_entry = _fire_summary(fire)
        ttr_entry["parts"] = time_to_recovery_parts(
            samples, item, fire_tick, budget, config=config
        )
        ttr_per_fire.append(ttr_entry)

    recovery_scoreable = scoreable_fires > 0
    rate = recovered / scoreable_fires if recovery_scoreable else None
    recovery = {
        "value": rate,
        "metadata": {
            "scoreable": recovery_scoreable,
            "item": item,
            "num_fires": len(fires),
            "recovered": recovered,
            "scoreable_fires": scoreable_fires,
            "fires": per_fire_recovery,
        },
    }

    ttr_scoreable = [e["parts"] for e in ttr_per_fire if e["parts"] is not None]
    time_to_recovery = {
        # Mean over scoreable fires, right-censored at each fire's budget.
        # A one-episode display number only: pool the per-fire (ticks,
        # recovered) pairs with wrench_core.survival across episodes.
        "mean_ticks": (
            sum(p["ticks"] for p in ttr_scoreable) / len(ttr_scoreable)
            if ttr_scoreable
            else None
        ),
        "fires": ttr_per_fire,
    }

    # -- Detection ---------------------------------------------------------
    det = detection_metrics(ledger_events, fires, config=config)
    counts = detection_counts(ledger_events, fires, config=config)
    latencies = det["latencies"]
    mean_latency = sum(latencies) / len(latencies) if latencies else None
    detection = {
        "value": det["recall"],
        "metadata": {
            "precision": det["precision"],
            "precision_strict": det["precision_strict"],
            "recall": det["recall"],
            "latencies": latencies,
            "mean_latency_ticks": mean_latency,
            **{
                k: counts[k]
                for k in (
                    "matched_reports",
                    "matched_reports_strict",
                    "num_reports",
                    "matched_fires",
                    "num_fires",
                )
            },
        },
    }

    scalars = {
        "throughput_retained": pooled,
        "throughput_retained_raw": num / den if scoreable else None,
        "throughput_retained_floor_adj": floor_pooled,
        "tr_scoreable": scoreable,
        "tr_pooled_numerator": num,
        "tr_pooled_denominator": den,
        "recovery_rate": rate,
        "recovery_scoreable_fires": scoreable_fires,
        "recovery_recovered": recovered,
        "time_to_recovery_ticks": time_to_recovery["mean_ticks"],
        "detection_recall": det["recall"],
        "detection_precision_strict": det["precision_strict"],
        "detection_precision": det["precision"],
        "detection_latency_ticks": mean_latency,
        "num_fires": len(fires),
        "num_reports": counts["num_reports"],
    }

    return {
        "item": item,
        "num_fires": len(fires),
        "throughput_retained": throughput_retained,
        "recovery": recovery,
        "time_to_recovery": time_to_recovery,
        "detection": detection,
        "scalars": scalars,
    }
