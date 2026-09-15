# wrench-compose-env

### Overview
- **Environment ID**: `wrench-compose-env`
- **Short description**: Disruption recovery on a docker-compose job pipeline. The agent is the on-call operator of gateway -> redis -> workers -> postgres and acts through `wrenchctl` from a locked-down sandbox container, one shell command per turn. Once the pipeline demonstrably meets its quota a seeded fault strikes (a container is killed, the worker->redis path is degraded, the database's connection slots are exhausted, or the most load-bearing component is taken out) without any announcement. Reward is how much throughput survived, measured by an HMAC-verifying probe the agent cannot reach.
- **Tags**: multi-turn, agent, sre, docker, disruption-recovery, eval, train
- **Source**: [WRENCH repository](https://github.com/nicknjpconsultingllc/wrench), `substrates/compose`. The Factorio substrate's environment (`wrench-factorio`) has the same turn protocol and the same metrics; one contract, two substrates.

### Prerequisite: Docker

Every rollout brings up its own pair of compose projects (the factory the agent sees and the admin side that loads, probes and disrupts it) on one *slot* and tears them down afterwards. From a checkout:

```bash
cd substrates/compose
make venv && make build                     # .venv + wrench-svc:local + wrench-agent:local
uv pip install -e environments/wrench_compose_env --no-deps
export WRENCH_COMPOSE_SLOTS=2               # concurrent episodes (default 1)
```

Slot `k` uses the subnets `10.232.k.0/24` and `10.231.k.0/24`; rollouts beyond `WRENCH_COMPOSE_SLOTS` wait for a free slot.

### Datasets
- **Primary dataset**: generated, one row per (fault kind, seed). Kinds: `entity_destruction`, `belt_cut`, `resource_exhaustion`, `adaptive_strike`, `silent_throttle`. Seed 1 hits the single point of failure (`gateway-1`), seed 3 one of two workers; `silent_throttle` (a postgres commit throttle that no container-status glance reveals) ignores the seed.
- **Split sizes**: `kinds x seeds` rows (default 5 x 1); train and eval are the same generated grid.

### Task
- **Type**: multi-turn (default 30 turns, one shell command line per turn, at least 6 s apart)
- **Output format**: exactly one ```` ```sh ```` block with one command line. Its stdout/stderr and exit code come back together with `wrenchctl ps` and `wrenchctl metrics`. A prose-only reply consumes the turn with a "no command" notice.
- **Rubric overview**: the reward is floor-adjusted Throughput Retained (the passive-redundancy floor of the surviving replicas removed) when the fire defines one, else plain winsorized Throughput Retained, pooled over every fired fault; everything else is a zero-weight metric.
- **Episode length**: ~200 s wall clock plus model latency (2.5-3.5 s bring-up, fault at ~61 s, 120 s measurement window, 7 s teardown). The episode always runs to the window end; turns after the window closes are not scored and, by default, not taken.

### Quickstart

```bash
uv run vf-eval wrench-compose-env -m gpt-4.1-mini -n 1 -r 1 -c 1 -a '{"kinds": "entity_destruction", "seeds": "3"}' -C wrench -s
```

All four kinds, two seeds each, two slots:

```bash
WRENCH_COMPOSE_SLOTS=2 uv run vf-eval wrench-compose-env -m <model> -n 8 -r 1 -c 2 -a '{"seeds": "1,3"}' -C wrench -s
```

### Environment Arguments

| Arg | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `kinds` | str or list | all four | Fault kind(s); comma-separated string accepted |
| `seeds` | str, int or list | `1` | Seed(s) per kind (rows = kinds x seeds) |
| `turns` | int | `30` | Turn budget per episode |
| `turn_period_s` | float | `6.0` | Minimum seconds between two executed commands |
| `max_context_messages` | int | `24` | Messages kept after the system prompt in each model call (0 = never trim) |
| `shaped_turn_rewards` | bool | `false` | Write the potential-based shaping delta of each turn into `trajectory[i]["reward"]` |
| `timeout_seconds` | float | none | Per-rollout wall-clock timeout (`MultiTurnEnv`) |

### Metrics

| Metric | Meaning |
| ------ | ------- |
| `reward` = `throughput_retained_reward` | `throughput_retained_floor_adj` when defined (`entity_destruction`, `adaptive_strike`), else `throughput_retained`. **0.0 when nothing fired.** |
| `throughput_retained` | Sum of post-fire jobs over sum of expected jobs (frozen pre-fire baseline x window), pooled over fires, winsorized to [-0.5, 1.5]. |
| `tr_scoreable` | 1 when at least one fire had a valid baseline. Filter on this before averaging. |
| `throughput_retained_raw` / `throughput_retained_floor_adj` | The unclamped ratio; the ratio with the passive-redundancy floor `(n-1)/n` removed from numerator and denominator, `n` = the victim service's replica count at fire time (`entity_destruction`, `adaptive_strike`; a single point of failure has floor 0). |
| `tr_pooled_numerator`, `tr_pooled_denominator` | Raw pooled parts; pool across rollouts as sum/sum. |
| `recovery_rate`, `time_to_recovery_ticks` | Fires back at >= 0.9x baseline for two consecutive samples; ms from fire to that point (right-censored at the window). |
| `detection_recall`, `detection_precision_strict`, `detection_precision`, `detection_latency_ticks` | `report_fault` naming the faulted service (strict) or one dependency hop away (loose); recall gated to 0 below 0.5 loose precision. |
| `num_fires`, `quota_met`, `steps_completed`, `command_rate`, `num_turns` | Episode bookkeeping. |

### Known limits

- **Sparse signal by design.** The fault arms only after the pipeline holds the quota; with the default load it always does (~61 s), but an agent that breaks the pipeline in its first minute is never disrupted and scores `tr_scoreable = 0`.
- **Pooling rule.** `vf-eval`'s `avg_metrics` are plain means; the benchmark number pools `tr_pooled_numerator` / `tr_pooled_denominator`.
- **Pre-fire overbuild earns zero recovery credit.** The floor counts the victim service's replicas at fire time, so replicas added before the fire raise the floor instead of the reward: the reward pays only for throughput above what the surviving replicas keep on their own, i.e. post-fire action. A no-op agent on a redundant service scores about 0; the same no-op on a single point of failure scores its plain TR (floor 0).
- **Ground truth stays hidden.** Observations never mention what was armed, when, or with which seed; `tests/test_prompt_leak.py` enforces it.

### Versions

Built and tested against `verifiers==0.2.1` (classic `MultiTurnEnv` API). Publish with `prime env push --path environments/wrench_compose_env --runtime v0`.
