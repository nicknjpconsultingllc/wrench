# Porting notes: fork `fle/disruptions/` to `wrench_core`

## What changed in `scoring.py`

Every function name, positional signature and return shape is the fork's.
Each public scorer gained one keyword-only parameter,
`config: ScoringConfig = DEFAULT_CONFIG`, and `DEFAULT_CONFIG` is the
Factorio substrate. `tests/test_fork_fixtures.py` checks three real fork
episodes for exact (`==`) equality against the fork's `episode_metrics`
output.

`ScoringConfig` (frozen dataclass) fields, with the fork's hard-coded value
as the default:

| field | default | replaces |
|---|---|---|
| `ticks_per_minute` | `3600` | module constant `TICKS_PER_MINUTE` (still exported) |
| `trailing_window_ticks` | `1800` | `window_ticks=1800` defaults in `throughput_series`, `recovery_potential`, `shaped_reward_delta`, and the bare `throughput_series(post_samples, item)` call inside `_first_sustained_recovery_tick` |
| `baseline_window_ticks` | `3600` | `window_ticks=3600` default in `frozen_baseline`, and the bare `frozen_baseline(samples, item, fire_tick)` calls inside the TR/recovery/potential scorers |
| `matcher` | `position_matcher` | `_matches`'s inlined Euclidean x/y test |
| `redundancy_kinds` | `frozenset({"entity_destruction"})` | `kind == "entity_destruction"` in `_redundancy_total` |

The plan named three parametrizations (tick rate, matcher, redundancy
kinds). The two window fields are the fourth: without them
`ticks_per_minute=60_000` would still run a 1.8-second trailing window and a
3.6-second baseline window through the internal calls, so a wall-clock
substrate could not actually use the tick-rate knob. The `window_ticks`
parameters on `throughput_series`, `frozen_baseline`, `recovery_potential`
and `shaped_reward_delta` now default to `None`, meaning "the config's
value"; an explicit integer behaves exactly as before.

The matcher contract: `matcher(report_entry, fire_event, scope) -> bool`,
where `scope` is a `MatchScope(radius, strict)`. `detection_counts` runs its
loose pass with `MatchScope(radius, strict=False)` and its strict pass with
`MatchScope(min(3.0, radius), strict=True)`, exactly the fork's two radii.
The tick check (report at or after the fire) stays in core; the matcher
only answers "is this report about this fire". The compose substrate keeps
`position_matcher`: its services sit on synthetic positions 20 units apart
(`wrench_compose/positions.py`), so both the strict (3) and the loose (10)
radius match exactly the named service and strict == loose there; which
services a fire names is the manifest's job (`wrench_compose/manifest.py`,
e.g. a `belt_cut` lists the workers, `netproxy` and `redis`).
`_report_position` / `_affected_positions` are unchanged and feed
`position_matcher`.

Private helper signatures changed (nothing outside `scoring.py` calls them):
`_matches(report_tick, report, fire_event, scope, matcher)`,
`_redundancy_total(fire_event, *, config)`,
`_first_sustained_recovery_tick(..., *, config)`.

Docstrings that pointed at fork paths (`server.lua`,
`tests/wrench/test_detection_gaming.py`, `wrench_scorers.py`) now describe
the Factorio substrate in prose; no logic in those paragraphs.

## Other modules

- `ledger.py`, `spec.py`: verbatim. `DisruptionKind` is still the Factorio
  enum; the compose substrate will need to add its kinds there (or the enum
  needs to become open), which is a substrate-side decision, not made here.
- `trajectory.py`: verbatim apart from two docstring references.
- `metrics.py` (new file, ported code): `episode_metrics`, `fired_events`,
  `tracked_item` moved out of the fork's `episode.py` unchanged, plus the
  `config` keyword threaded through. This is the function the fixture test
  compares against, and the entry point the compose substrate calls.
- `survival.py` (new): `kaplan_meier` over `(ticks, recovered)` pairs, a
  `SurvivalCurve` with `survival_at`, `median`, `restricted_mean(tau)` and
  `points()`, `recovery_data` to pull per-fire pairs out of
  `episode_metrics` outputs, and `pooled_time_to_recovery` for the
  cross-episode summary. `docs/benchmark_design.md` in the fork promised
  this curve; `scripts/run_table.py` still reports only the recovery rate.

## Tests

- `tests/test_scoring.py`: the fork's `tests/wrench/test_scoring.py` with
  `fle.disruptions.*` imports and docstring references rewritten.
- `tests/test_trajectory.py`: the fork's, import rewritten.
- `tests/test_metrics.py`: `TestEpisodeMetrics` from the fork's
  `tests/wrench/test_episode.py` (the rest of that file needs `fle.eval`).
- `tests/test_detection_gaming.py` was not ported: it is `wrench_live` and
  drives a Factorio server on port 27001. Its synthetic twin already lives
  in `test_scoring.py::test_report_spam_gets_no_extra_precision_credit`.
- `tests/fixtures/*.json`: three episodes from
  `table_runs/20260810T125411/logs`, each carrying `samples` (trimmed to
  ticks after `first_fire - 2 * 3600`; the dump script asserted the fork's
  output is identical on the trimmed and full data), `ledger_events`,
  `quota_item`, `end_tick` and the fork's `episode_metrics` output under
  `expected`. Source log and sample index are recorded under `source`.

## Fork-side diff to consume core

Add `wrench-core` to the fork's dependencies (path dependency during
development), then change imports only:

```
fle/disruptions/episode.py
-from fle.disruptions.scoring import (...)
+from wrench_core.scoring import (...)
-from fle.disruptions.trajectory import TrajectoryWriter
+from wrench_core.trajectory import TrajectoryWriter
 # optionally delete fired_events/tracked_item/_fire_summary/episode_metrics and
+from wrench_core.metrics import episode_metrics, fired_events, tracked_item
 # (wrench_scorers.py and test_episode.py import episode_metrics from episode.py;
 #  the re-export keeps them working)

fle/disruptions/__init__.py
-from fle.disruptions.ledger import EventLedger, LedgerEntry
-from fle.disruptions.spec import DisruptionKind, DisruptionSpec, Precondition
+from wrench_core.ledger import EventLedger, LedgerEntry
+from wrench_core.spec import DisruptionKind, DisruptionSpec, Precondition

fle/eval/inspect/integration/solver.py:320
-            from fle.disruptions.trajectory import TrajectoryWriter
+            from wrench_core.trajectory import TrajectoryWriter

scripts/wrench_pilot.py
-from fle.disruptions.scoring import (...)
-from fle.disruptions.trajectory import TrajectoryWriter
+from wrench_core.scoring import (...)
+from wrench_core.trajectory import TrajectoryWriter

scripts/run_table.py
-from fle.disruptions.scoring import winsorize_tr  # noqa: E402
+from wrench_core.scoring import winsorize_tr  # noqa: E402

tests/wrench/test_adaptive_strike.py, test_bracketing.py,
test_floor_acceptance.py, test_detection_gaming.py,
test_observability_budget.py, test_episode.py
-from fle.disruptions.scoring import ...
+from wrench_core.scoring import ...

tests/wrench/test_scoring.py, tests/wrench/test_trajectory.py
 delete (they run here as tests/test_scoring.py, tests/test_trajectory.py)
```

Then delete `fle/disruptions/scoring.py`, `ledger.py`, `spec.py`,
`trajectory.py`. `fle/disruptions/episode.py` (WrenchEpisode, the server
pool, prompt building) stays in the fork: it is the Factorio substrate.
`fle.disruptions` keeps re-exporting `DisruptionKind`, `DisruptionSpec`,
`EventLedger`, `LedgerEntry`, `Precondition`, so `disruption_task.py`,
`sentinel_tasks.py` and the task tests need no change.

No call site passes `config`, so every number the fork produces is
unchanged.
