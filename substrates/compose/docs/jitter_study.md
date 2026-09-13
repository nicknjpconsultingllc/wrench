# Jitter study: is the compose substrate deterministic enough to score?

Go/no-go question: replaying the same seeded disruption against the same
factory with a no-op agent, does Throughput-Retained come back the same?
Acceptance: TR coefficient of variation <= 0.05 across 10 replays per kind.

## Setup

- Host: Apple Silicon Mac, Docker Desktop 29.7 (arm64 VM, 18 CPUs, 8 GB),
  Compose v5.3. A Factorio headless server container belonging to another
  project was running on the same VM throughout.
- Factory: gateway, redis, 2 workers (1,000,000 sha256 iterations per job),
  postgres (max_connections=20). Load: 8 jobs/s open-loop, seeded job ids.
- Precondition: trailing 60 s rate >= 400 jobs/min on 2 consecutive 500 ms
  samples. Measurement window: 120 s after the fire. Tick = ms since probe
  start.
- `entity_destruction` seed 3 -> `worker-1` (one of two; the interesting case,
  because the no-op TR is set by the surviving worker's CPU-bound capacity,
  which is where scheduling jitter would show up). `belt_cut` seed 1 ->
  Toxiproxy latency toxic 1000 ms on the worker->redis hop (every redis
  round trip costs a second; two round trips per job).
- Scoring: `wrench_core.scoring` with the compose config (1 tick = 1 ms,
  60000-ms baseline window, 30000-ms trailing window;
  `wrench_compose/scoring.py`). The study originally ran through a shim over
  the fork's `scoring.py`; re-scoring all 48 runs through `wrench_core`
  reproduced every number exactly.
- All episodes ran serially on one slot; no other WRENCH episode was running.

Raw data: `runs/jitter_J1_<kind>_<agent>_<i>/{samples.jsonl,ledger.jsonl,episode.json}`.
Regenerate the table with
`python -m wrench_compose summarize runs/jitter_J1_* --out docs/jitter_table.md`.

## Results

| kind | agent | n | baseline mean (jobs/min) | baseline SD | TR mean | TR SD | TR CV | fire tick mean (s) | fire tick SD (s) | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| belt_cut | noop | 10 | 480.5 | 0.88 | 0.1095 | 0.0004 | 0.0038 | 60.9 | 0.24 | 0 |
| belt_cut | oracle | 3 | 480.7 | 0.54 | 0.9986 | 0.0011 | 0.0011 | 60.8 | 0.28 | 0 |
| entity_destruction | noop | 10 | 480.5 | 0.85 | 0.7451 | 0.0333 | 0.0448 | 60.8 | 0.26 | 0 |
| entity_destruction | oracle | 3 | 480.4 | 0.59 | 0.9992 | 0.0012 | 0.0012 | 60.8 | 0.29 | 0 |

Per-run numbers: `docs/jitter_table.md` (generated) and `runs/jitter_J1_*/episode.json`.
Wall clock per episode: 193-194 s (2.5-3.5 s bring-up, fire at 60.5-61.0 s,
120 s window, 7 s teardown).

### Floor and bracketing (one episode per cell, `runs/floor_20260912T115941_*`)

| kind | seed / victim | noop TR | restart_all TR | oracle TR | oracle recovered (TTR) | floor (<= 0.2 for noop and restart_all) |
|---|---|---|---|---|---|---|
| entity_destruction | 1 -> gateway-1 (SPOF) | 0.001 | 0.001 | 0.996 | yes (31.0 s) | pass |
| belt_cut | 1 -> latency 1000 ms on worker->redis | 0.109 | 0.090 | 1.000 | yes (31.0 s) | pass |
| resource_exhaustion | 1 -> pghog holds 16 slots | 0.002 | 0.007 | 1.000 | yes (31.0 s) | pass |
| adaptive_strike v1 (most in-flight work) | 1 -> worker-2 (holding 1 job) | 0.719 | 0.717 | 1.000 | yes (30.5 s) | **fail** |
| adaptive_strike v2 (most load-bearing: flow share / replicas) | 1 -> gateway-1 (score 1.0; redis 1.0, workers 0.25) | 0.002 | 0.002 | 0.992 | yes (30.5 s) | pass |

v2 runs: `runs/floor_20260912T125452_adaptive_strike_*`.
Detection for every oracle cell: precision_strict 1.0, recall 1.0, latency
285-441 ms (the oracle reports before it repairs). Every no-op / restart_all
cell: recall 0.0, precision 1.0 (vacuous, no reports).

### Oracle through the sandbox (M3, `runs/floor_20260912T174905_*_oracle`)

The oracle now acts only as `docker exec <sandbox> wrenchctl --json ...`
(no admin path, no host-side HTTP client), at the `cputime` default. Same
seeds as the table above.

| kind | victim | oracle TR | recovered (TTR) | detection latency | wrenchctl calls (ms) |
|---|---|---|---|---|---|
| entity_destruction | gateway-1 | 0.985 | yes (30.5 s) | 818 ms | report_fault 272, scale gateway 1: 404 |
| belt_cut | latency 1000 ms on worker->redis | 0.996 | yes (30.5 s) | 564 ms | report_fault 269, config edit worker REDIS_URL: 653 |
| resource_exhaustion | pghog holds 16 slots | 0.996 | yes (30.5 s) | 607 ms | report_fault 228, exec postgres psql: 329 |
| adaptive_strike v2 | gateway-1 (score 1.0) | 0.987 | yes (30.5 s) | 746 ms | report_fault 291, scale gateway 1: 401 |

Every cell recovers and detects at precision 1.0 / recall 1.0, so the
agent-visible surface is sufficient for all four kinds. Two shifts against
the host-client numbers: detection latency grew from 285-441 ms to
564-818 ms (one `docker exec` round trip, ~230-290 ms, in front of every
call), and the two gateway-kill cells dropped from 0.992-0.996 to
0.985-0.987 because the rebuilt gateway comes up ~0.4 s later for the same
reason and the open-loop loadgen loses those jobs. Both are the cost of the
real boundary, not noise.

### Fixed-CPU-time workers (5 no-op replays, `runs/jitter_C1_*`)

| kind | agent | n | baseline mean (jobs/min) | baseline SD | TR mean | TR SD | TR CV | fire tick mean (s) | fire tick SD (s) |
|---|---|---|---|---|---|---|---|---|---|
| entity_destruction (`WORK_MODE=cputime`, 195 ms) | noop | 5 | 481.4 | 0.91 | 0.6267 | 0.0017 | 0.0027 | 60.9 | 0.22 |

Post-fire jobs per 30 s slice, all five replays: 149-154 (iteration mode:
157-188).

### Replay at the new default (M3: `cputime` default, sandbox + wrenchctl, 3 no-op replays, `runs/jitter_M3cpu_*`)

Same kind, seed and topology as the C1 rows, after `WORK_MODE=cputime` became
the default and the agent boundary went in (chaos now samples the factory's
own metrics once a second, workers serve `/metrics`, the sandbox container
runs alongside the factory; the no-op agent does nothing, so these rows
measure only what the new default plus that background load does to the
number).

| kind | agent | n | baseline mean (jobs/min) | baseline SD | TR mean | TR SD | TR CV | fire tick mean (s) | fire tick SD (s) | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| entity_destruction (`cputime` default, 195 ms) | noop | 3 | 482.0 | 0.00 | 0.6224 | 0.0000 | 0.0000 | 61.0 | 0.00 | 0 |

Per run: TR 0.62241 / 0.62241 / 0.62241, 600 post-fire jobs in every 120 s
window (expected 964.0 at the 482.0 baseline), fire at tick 61000 in all
three. The survivor settles at exactly 300 jobs/min (one job per 200 ms
wall: 195 ms of CPU plus ~5 ms of redis + postgres I/O), which is why the
three windows agree to the job. The C1 mean was 0.6267 +/- 0.0017; the level
moved by 0.004 (the 1 Hz metrics poll on each worker is inside the process's
CPU budget, so it costs a little wall time per job) and the spread went to
zero at this n. The CV claim holds at the new default with margin.


## Reading

**Go.** Both kinds clear TR CV <= 0.05 with a no-op agent (entity_destruction
0.045 in the original iteration mode, 0.0027 with fixed CPU time, 0.0000 at
the shipped `cputime` default; belt_cut 0.004), the frozen baseline is 480.5 +/- 0.9 jobs/min (the
open-loop 8 jobs/s, as designed), and the fire lands at 60.8 +/- 0.26 s: the
precondition arms on the first two samples after the 60 s window fills, so the
only timing noise is the half-second sample grid plus container start skew.
The oracle brackets at 0.999 +/- 0.001 on both kinds.

entity_destruction sits close to the bar, and the noise has one source. After
the fire, throughput is the surviving worker's CPU-bound rate, and that rate
differs between episodes (post-fire jobs in 30 s slices: 187/187/187/188 in
run 06 vs 162/157/162/160 in run 08, a 17% gap that holds for the whole
window) and occasionally drifts within one (run 04: 186 -> 164). Queue
buffering is ruled out (redis lag stays at 0-1 with a job every 125 ms and a
worker every ~210 ms), loadgen is ruled out (baseline SD 0.85 jobs/min, no
failed sends), and CFS throttling is ruled out (limits are 2 CPUs for a
single-threaded process). What is left is where the VM's vCPU lands on an
Apple Silicon host: a run whose survivor sits on an efficiency core does the
same 1,000,000 sha256 iterations ~17% slower, and a migration shows up as a
mid-window step. belt_cut has no CPU term (throughput is set by the 1 s
toxic latency per round trip), which is why its CV is an order of magnitude
lower.

The fix that keeps the job CPU-bound is to fix CPU *time* per job instead of
iterations (`WORK_MODE=cputime`: spin until `process_time` advances 195 ms).
Wall time per job then no longer depends on which core runs it, only on
contention. Five replays in that mode: TR 0.6267 +/- 0.0017, CV 0.0027, a 17x
reduction, with the survivor's 30 s slices flat at 149-154 jobs across all
five runs. The TR level moves (0.745 -> 0.627) because 195 ms of CPU time is
more work than 1,000,000 iterations on a fast core; that is a calibration
constant, not noise. Recommendation for the substrate: make `cputime` the
default worker mode and keep the iteration mode as the documented example of
host-scheduling jitter.

Two other things the study surfaced:

- adaptive_strike v1 ("kill the holder of the most in-flight work") failed
  the floor. With capacity above load the queue is empty at fire time, so the
  holder is always one worker holding one job, and the strike is a
  redundant-worker kill (TR 0.72, the same failure the Factorio
  adaptive_strike had, taxonomy F11). v2 ranks holders by the capacity their
  loss removes (10 s flow-through share / replica count,
  `wrench_compose.pick.load_bearing`): gateway and redis score 1.0, each of
  two workers 0.25, postgres is excluded as stateful. Seed 1 breaks the
  gateway/redis tie toward gateway. All three v2 cells pass. On a 4-service
  factory this coincides with the SPOF entity_destruction cell; the
  definitions only diverge once workers=1 or a service is scaled.
- The v1 in-flight sampler saw one worker with pending=1 at every sample and
  the other at 0 in all three cells (expected ~1 each). Not diagnosed; v2 no
  longer uses that signal for ranking (it is still recorded in the manifest as
  `inflight_all`).

