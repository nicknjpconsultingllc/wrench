# WRENCH compose substrate

A docker-compose job pipeline as the system WRENCH disrupts. Same measurement
contract as the Factorio substrate (`fle/disruptions/scoring.py`): cumulative
samples `{"tick": ms_since_episode_start, "counts": {"jobs_done": n}}`, a
`LedgerEntry`-shaped JSONL ledger, precondition-gated arming, seeded faults,
a fixed post-fire window, run-to-window-end.

```
loadgen (admin) --HTTP--> gateway --XADD--> redis <--XREADGROUP-- worker xN --INSERT--> postgres
                                             ^                        |
                                             +---- netproxy (Toxiproxy, admin) ----+
probe (admin) --SELECT--> postgres        chaos (admin, holds the Docker socket)
agent (sandbox) --wrenchctl--> wrenchapi:8081 (chaos, factory-network listener)
```

Two compose projects on two networks. `compose/factory.yml` is what the
agent sees (labels `wrench.role=factory`, `restart: "no"` everywhere, CPU and
memory limits, pinned image digests) plus the `agent` sandbox. `compose/admin.yml`
is loadgen, probe, chaos and Toxiproxy. loadgen, probe and Toxiproxy also
attach to the factory network because they must touch it, but every listener
on the admin side binds to a static admin-network address (Toxiproxy control
`.10:8474`, probe `.11:8080`, chaos admin routes `.20:8080`); chaos also joins
the factory network as `wrenchapi` (`10.232.<slot>.20`) and serves only the
`/agent/*` routes there. No container publishes a port: a published port is
reachable from every bridge on the host (masqueraded to the gateway address,
so the server cannot tell it from host traffic) and via `host.docker.internal`
on Docker Desktop, so the episode runner reaches the probe through
`docker exec` instead.

Throughput is HMAC-verified rows per minute: loadgen stamps every job id with
an HMAC whose key exists only in the admin project; the probe counts a row
only when the stamp verifies. Nothing inside the factory can mint throughput
(`tests/test_partition.py::test_rows_minted_in_postgres_do_not_count`).

## Run

```bash
make venv            # uv venv + pip install -e .[dev] -e ./wrenchctl
make build           # wrench-svc:local (all admin/factory Python services) + wrench-agent:local (the sandbox)
make test            # 86 unit tests, no Docker (inspect-ai + verifiers installed by make venv)
make test-partition  # 9 anti-gaming checks from inside the sandbox, ~1 min with Docker
make test-live       # every compose_live test: partition + bracketing + 4 no-op floor cells, ~25 min
make demo            # no-op vs oracle on entity_destruction, ~8 min
make jitter          # 10 no-op replays x 2 kinds + 3 oracle replays x 2 kinds
make floor           # 4 kinds x {noop, restart_all, oracle}
make test-driver     # LLM path end to end with mockllm + the scripted operator, ~7 min with Docker
make table-smoke     # run_table.py with mockllm on 2 kinds x 1 seed, 2 slots
.venv/bin/python -m wrench_compose run --name x --kind belt_cut --seed 1 --agent oracle
.venv/bin/python -m wrench_compose score runs/x
```

Scoring is `wrench_core.scoring` with the compose `ScoringConfig`
(`wrench_compose/scoring.py`): one tick is one millisecond, so the 1-minute
baseline and 30-second trailing windows are 60000 and 30000 ticks. The same
functions score the Factorio substrate with the default config.

Each run writes `runs/<name>/samples.jsonl`, `ledger.jsonl`, `episode.json`
(config, timing, fired manifest, agent actions, scores) and the compose logs.

## Episode

1. The runner writes the run's secrets to `runs/<name>/.secrets/`
   (`hmac.key`, `analytics.pw`; 0600, gitignored, deleted at teardown) and
   points `WRENCH_SECRETS_DIR` at it. `factory.yml up --wait`, then
   `admin.yml up`. Tick 0 = probe start. The runner waits until
   `wrenchctl ps` answers from inside the sandbox.
2. Runner arms one spec on the probe: quota 400 jobs/min, fraction 1.0, two
   consecutive 500 ms samples whose trailing 60 s rate meets it.
3. The probe fires through `chaos` and records `armed`/`fired` with the
   affected manifest (`service`, `container`, synthetic `x,y`,
   `same_type_total` = replicas of that service at fire time, kind extras).
4. Runner hands the fired event to the fixture agent, which acts only through
   `docker exec <sandbox> wrenchctl ...`, then waits until
   `tick >= fire_tick + 120 s` no matter what the agent does.
5. Both projects come down; the secrets directory is removed; files are
   written; `wrench_core` scores.

Defaults: 8 jobs/s open-loop, 2 workers, 195 ms of CPU time per job
(`WRENCH_WORK_MODE=cputime`, `WRENCH_WORK_CPU_MS=195`; ~15 ms I/O on top, so
one worker caps near 300 jobs/min and misses the 400 quota; two workers are
load-limited at 480 = 1.2x quota). `WRENCH_WORK_MODE=iters` is the old
default (1,000,000 sha256 iterations, `WRENCH_WORK_ITERS`); the jitter study
shows why cputime replaced it (no-op TR CV 0.045 -> 0.003).

## Contract mapping

| Factorio kind | Compose fault | Executor | Oracle repair (agent surface only) | Why `restart_all` fails |
|---|---|---|---|---|
| `entity_destruction` | seeded pick over running `gateway`/`worker` containers (sorted by name, server.lua's LCG), `docker kill` + `rm` | chaos, docker SDK | `scale <service> <n>` rebuilds from the blueprint | container is gone; `restart` returns 404 |
| `belt_cut` | Toxiproxy `latency` 1000 ms (or `timeout`) toxic on the worker->redis proxy `netproxy` | chaos -> Toxiproxy API | `config worker REDIS_URL=redis://redis:6379/0` (re-route around the cut) | toxic lives in Toxiproxy, an admin container the agent cannot restart |
| `resource_exhaustion` | admin `pghog` container greedily holds every free Postgres slot as role `analytics` (max_connections=20) and re-grabs after a restart | chaos runs the hog container | `exec postgres psql -c "ALTER ROLE analytics CONNECTION LIMIT 0; SELECT pg_terminate_backend(...)"` | hog reconnects faster than the workers after every Postgres restart |
| `adaptive_strike` | probe measures 10 s flow-through per holder (every committed job traversed `gateway` and `redis`; each worker its own rows); chaos scores `flow share / replica count` (the capacity the loss removes: a SPOF scores 1.0, one of two workers ~0.25), kills the top holder, ties by seed. Postgres is excluded (stateful). | chaos | `scale <service> 1` | container is gone |

On the default topology `adaptive_strike` therefore kills `gateway` or `redis`
(seed breaks the tie). The first version ranked holders by in-flight work
instead; with capacity above load the queue is empty at fire time, so that
always picked a worker holding one job and failed the floor test (TR 0.72).
The before/after is in `docs/jitter_study.md`.

Detection: the fork's scorer matches `report_fault` to fires by position
radius, so services have fixed synthetic positions 20 units apart
(`wrench_compose/positions.py`); `report_fault <service>` lands on that
service's position, strict radius 3 matches only the right service. A
`belt_cut` manifest lists the workers plus `netproxy` and `redis`
(`wrench_compose/manifest.py`), so a report naming any of the three earns
credit; the agent cannot tell which end of the hop is at fault.

Seeds against the default 2-worker layout (candidates sorted:
`gateway-1, worker-1, worker-2`): seed 3 picks `worker-1` (redundant,
no-op TR ~0.6, `same_type_total=2`, used for bracketing and the jitter
study); seed 1 picks `gateway-1` (single point of failure, used for the floor
test), mirroring the fork's two-furnace bracketing build vs. its SPOF floor
build.

## Agent surface: the sandbox and `wrenchctl`

The agent runs inside the `agent` service of the factory project
(`services/Dockerfile.agent`: `python:3.12-slim` + the `wrenchctl` package,
nothing else). It is uid 10001, read-only root filesystem with a tmpfs `/tmp`,
`cap_drop: ALL`, `no-new-privileges`, 0.5 CPU / 64 MB / 64 pids, attached to
the factory network only, no Docker socket, no secrets, no docker CLI. It
carries `wrench.role=agent`, so the agent API does not list it and cannot
exec into, restart or scale it.

`wrenchctl` (`wrenchctl/`, stdlib only, `pip install ./wrenchctl` on the host
for the unit tests) is the only tool. Every verb is one HTTP call to
`$WRENCH_AGENT_API` (`http://wrenchapi:8081`), chaos's factory-network
listener; chaos does the Docker work on the agent's behalf, so the agent never
holds a socket. `--json` prints the raw response (what the fixtures parse).

| verb | API | notes |
|---|---|---|
| `ps` | `GET /agent/ps` | factory containers only (`wrench.role=factory` in the factory project, `wrench_compose/allowlist.py`) |
| `logs <service> [--tail N]` | `GET /agent/logs` | |
| `exec <service> -- <cmd...>` | `POST /agent/exec` | runs in that service's first running container via the SDK; refuses any non-factory service with 404 |
| `restart <service>` | `POST /agent/restart` | a removed container cannot be restarted (`scale` rebuilds it) |
| `scale <service> <n>` | `POST /agent/scale` | 0..4, enforced server-side; rebuilds from the blueprint chaos snapshotted at startup |
| `config edit <service> KEY=VALUE...` | `POST /agent/config` | env change + recreate |
| `metrics` | `GET /agent/metrics` | the factory's own view, sampled by chaos every second: gateway `admitted/rejected/inflight` (+ trailing 30 s rate), redis stream `length/pending/lag`, each worker's `done` (workers serve `/metrics` on 8000), `jobs_done` and `jobs_done_per_min` accumulated as per-container deltas so a restart does not drop the totals. Never the probe's samples. |
| `report_fault <service> <cause>` | `POST /agent/report_fault` | lands on that service's synthetic position |

Every mutating call appends an `agent_action` ledger event (chaos -> probe
over the admin network). What an LLM agent is shown is
`wrench_compose/prompt.py::system_prompt` (the objective, the services,
`wrenchctl --help`); `tests/test_prompt_leak.py` asserts that text, every
verb's help and the `ps`/`metrics`/... output formats never mention a fault
kind, seed, ledger vocabulary, HMAC or an admin service name.

Fixture agents (`wrench_compose/agents.py`): `noop`, `restart_all` (restarts
every factory service every 15 s from the fire), `oracle` (reads the fired
manifest and applies the repair in the table above). All three act through
`Wrenchctl`, i.e. `docker exec wrench-factory-<slot>-agent-1 wrenchctl --json ...`,
never through an admin path, so a passing bracket proves the sandbox's
surface is sufficient to recover from every kind.

## LLM driver: `ComposeEpisode`, the Inspect task, the table

`wrench_compose/episode.py::ComposeEpisode` is the LLM path, with the same
turn protocol as the Factorio substrate's `WrenchEpisode`
(`fle/disruptions/episode.py`): `start()` brings the stack up and arms the
spec, `system_prompt()`, `observe()` -> text, `step(command)` -> feedback,
`skip_step` / `fail_step` for the two model-side failure paths, `is_done`,
`drain()`, `finalize()` -> dict, `cleanup()`. One driver on each side of the
contract, one class in the middle.

- **Action** = one shell command line, run as
  `docker exec <sandbox> timeout 120 sh -c <command>` (so `wrenchctl ...`
  plus ordinary shell), stdout/stderr returned with a 6000-byte cap each and
  the exit code. The reply's first fenced block supplies the command
  (`parse_command`); a prose-only reply consumes the turn with a
  "no command" notice, exactly as a no-code-block reply does in Factorio.
- **Observation** = last feedback + `wrenchctl ps` + `wrenchctl metrics`,
  rendered by the sandbox's own `wrenchctl`. Never the probe's samples or
  the ledger; `tests/test_prompt_leak.py` renders the full system prompt, an
  observation from a canned episode after its fire, and every `wrenchctl`
  output format, and asserts none of them names a fault kind, a seed, the
  ledger, HMAC or an admin service.
- **Turns**: budget 30 (`turns=`), at least 6 s between two executed
  commands (`turn_period_s=`; 30 x 6 s covers the 60 s arming baseline plus
  the 120 s window even when the model answers instantly). The episode never
  ends early on quota; `finalize()` waits for the fire and then to the
  window end whatever the agent did, so a five-turn mockllm episode and a
  thirty-turn real one are scored on the same window. By default the turn
  loop stops once the window has closed (`stop_after_window=True`): nothing
  after it is scored, so those turns would only cost money.
- **Scoring**: `wrench_core.metrics.episode_metrics` with `COMPOSE_CONFIG`,
  `end_tick` clipped to `fire_tick + window_ms`. `finalize()` returns the
  fork's dict shape (samples, ledger events, fires, quota_met, end_tick,
  shaped rewards, `metrics` scalars, `scores` blocks) and writes
  `runs/<name>/{samples.jsonl,ledger.jsonl,episode.json}` so
  `python -m wrench_compose score` works on LLM runs too.

### Inspect

```bash
.venv/bin/inspect eval wrench_compose/inspect_task.py@compose_sentinel \
    -T kinds=entity_destruction,belt_cut -T seeds=1,3 -T turns=30 \
    --model openrouter/anthropic/claude-sonnet-4.6
.venv/bin/inspect eval wrench_compose/inspect_task.py@compose_sentinel \
    -T kinds=entity_destruction -T seeds=3 -T turns=5 --model mockllm/model      # no API key
.venv/bin/inspect eval wrench_compose/inspect_task.py@compose_sentinel \
    -T kinds=entity_destruction -T seeds=3 --model compose-scripted/operator     # scripted repair policy
```

`wrench_compose/inspect_task.py` mirrors `fle/eval/inspect/wrench.py`: the
solver is Inspect's message/generate loop around `ComposeEpisode` (system
prompt + the most recent 24 messages per call, `max_tokens` 4096 with a
per-model reasoning cap: `reasoning_effort=low` for OpenAI reasoning models,
a 1024-token thinking budget for Anthropic/Gemini; bounded retries), it
copies the episode into a `ComposeData` store after every turn, and four
consecutive empty completions abandon the episode as an error row instead
of burning the remaining turns. The
scorers `throughput_retained` / `recovery` / `detection` are pure readers of
that store through `episode_metrics` and carry the same value/metadata
shapes as the fork's (`pooled_numerator` / `pooled_denominator`,
floor-adjusted pair, per-fire breakdowns, detection counts). One episode per
compose slot: `WRENCH_COMPOSE_SLOTS` (default 1) sizes
`wrench_compose/slots.py::slot_pool`, slot `k` owning `10.232.k.0/24` and
`10.231.k.0/24`; samples past that count wait for a free slot.

`compose-scripted/operator` (`wrench_compose/policy.py`, registered as an
Inspect model provider through the `inspect_ai` entry point) is a scripted
operator that reads only what a model would (system prompt, observations,
its own replies) and answers one `wrenchctl` command per turn: a service
below its replica count is rebuilt with `scale` and then reported; a
throughput collapse with every replica present reads the worker logs once
and re-routes the worker->redis hop or revokes the `analytics` role's
connections. It proves that the model path (reply -> sandbox shell ->
probe) can recover, not just the fixture agents that read the fired
manifest.

### The comparison table

```bash
WRENCH_COMPOSE_SLOTS=2 .venv/bin/python scripts/run_table.py \
    --models openrouter/anthropic/claude-sonnet-4.6,openrouter/openai/gpt-5-mini \
    --kinds entity_destruction,belt_cut,resource_exhaustion,adaptive_strike --seeds 1,3
.venv/bin/python scripts/run_table.py --models mockllm/model --kinds entity_destruction,belt_cut --seeds 3 --turns 5
```

`scripts/run_table.py` is the fork's `scripts/run_table.py` over the compose
task: models x kinds x seeds through `inspect_ai.eval_set` (interrupted runs
resume with `--resume <run dir>`), per-episode rows, per-(model, kind)
aggregates by the pooled-ratio rule (sum numerators / sum denominators,
never mean of ratios), time-to-recovery pooled through
`wrench_core.survival.pooled_time_to_recovery` (Kaplan-Meier median and
restricted mean per (model, kind), censored fires kept at risk until the
window end), Markdown + JSON under `table_runs/<stamp>/` and the episodes'
run directories under `table_runs/<stamp>/episodes/`. An episode is an
error row when `sample.error` is set **or** the store's `ComposeData:error`
is non-empty: the solver returns normally after an infrastructure failure,
so `sample.error` alone would report it as a genuine 0-fire success (the
fork found this the hard way).

### verifiers

```bash
uv pip install -e environments/wrench_compose_env --no-deps
WRENCH_COMPOSE_SLOTS=2 vf-eval wrench-compose-env -m <model> -n 8 -r 1 -c 2 -a '{"seeds": "1,3"}' -C wrench -s
```

`environments/wrench_compose_env/` mirrors `environments/wrench_factorio`
in the fork: a `MultiTurnEnv` around `ComposeEpisode` (dataset = kinds x
seeds, reward = pooled winsorized TR, every other metric at weight 0,
`tr_scoreable` to filter on), so the Hub can host both substrates.

### Cost and time

An episode is ~200 s wall clock (2.5-3.5 s bring-up, fault at ~61 s, 120 s
window, 7 s teardown) plus model latency, on one slot; `WRENCH_COMPOSE_SLOTS`
runs that many in parallel (each stack wants ~4 CPUs). A turn is one
command, so context stays small: the system prompt is ~860 tokens
(cl100k), an observation ~350, a reply ~15; with the 24-message window a
model call is at most ~5.3k prompt tokens and a full 30-turn episode sends
~145k prompt tokens in total (~4.8k per turn on average). With the default
`stop_after_window`, a model that answers in 5-10 s takes 15-20 turns before
the window closes, so 70-100k prompt tokens per episode is the realistic
figure.

## Run-time secrets

Nothing secret is checked in. Per run, `wrench_compose.episode.write_secrets`
generates `runs/<name>/.secrets/hmac.key` (64 hex chars) and `analytics.pw`,
and `admin.yml` mounts them as Docker secrets:

- `hmac_key` -> loadgen (signs) and probe (verifies). No factory container
  has it in env or filesystem; the factory image never reads it
  (`test_hmac_key_absent_from_every_factory_container`). There is no
  default: a missing or short key file makes loadgen/probe exit.
- `analytics_password` -> chaos only. `postgres-init.sql` creates the
  `analytics` role with `LOGIN` and no password; chaos runs
  `ALTER ROLE analytics PASSWORD ...` at startup and again right before each
  hog launch (a recreated postgres starts from the init SQL), and hands the
  DSN to the `pghog` container it starts. The agent can still revoke the role
  from inside postgres, which is the repair.

`teardown` deletes the directory; `runs/*/.secrets/` is gitignored.

## Anti-gaming tests (`tests/test_partition.py`, `compose_live`)

One stack, no armed spec, every assertion made from inside the sandbox:
no wrench container publishes a port; `probe`, `toxiproxy`/`netproxy`
control, `loadgen`, `wrenchapi:8080` and the admin-network addresses are
connection-refused or unroutable while `netproxy:6379` and `wrenchapi:8081`
connect and `/chaos/fire` on the agent listener is 404; `wrenchctl exec`,
`logs`, `restart`, `scale`, `config` refuse `probe`, `loadgen`, `chaos`,
`toxiproxy`, `pghog` and `agent`; `scale worker 100` is refused and
`scale worker 4` works; 1000 rows inserted through `wrenchctl exec postgres`
raise the probe's `rejected` count by exactly 1000 and its verified count
by no more than the load rate; loadgen keeps running; the sandbox has no
socket, no `/run/secrets`, no docker CLI, uid 10001, read-only root; no
factory container has the HMAC key; `metrics` carries the factory keys and
nothing named after the probe.

## What is still stubbed

- The scripted operator (`compose-scripted/operator`) repairs replica loss
  from `ps` alone; its `belt_cut` / `resource_exhaustion` branches key off
  worker logs and are exercised only by unit tests so far.
- One image (`wrench-svc:local`) for gateway/worker and for the admin
  services, so a factory container's filesystem holds the admin services'
  source (not their secrets). A split image would remove that.
- Toxiproxy's control port is bound to the admin IP, but `netproxy:6379` is
  on the factory network by design (it is the belt).
- One `pghog` per fire; a second `resource_exhaustion` in the same episode
  replaces it.
- `restart_all`'s restart of `redis` drops the stream (no persistence) and of
  `postgres` recreates nothing (data survives in the container filesystem).

## Results

`docs/jitter_study.md`: in the original iteration mode, 10 no-op replays
per kind gave TR CV 0.045 (`entity_destruction`, worker victim) and 0.004
(`belt_cut`); at the shipped `cputime` default, 3 no-op replays of the
worker kill give TR 0.6224 in all three (CV 0.0000, 600 post-fire jobs each),
baseline 482.0 jobs/min, fire at 61.0 s. The oracle acting only through the
sandbox's `wrenchctl` recovers every kind: TR 0.985 / 0.996 / 0.996 / 0.987
(entity_destruction, belt_cut, resource_exhaustion, adaptive_strike),
detection precision 1.0 / recall 1.0, latency 564-818 ms. Floor and
bracketing tables for all four kinds are in the same file, with the raw runs
under `runs/`.

LLM path (`make test-driver`, `make table-smoke`, seed 3 = worker-1
victim): `mockllm/model` through the Inspect task, 5 turns, scores exactly
like the no-op replay (TR 0.621 = 599/964, recovery 0, recall 0,
`ComposeData.error` empty, 196 s wall). `compose-scripted/operator`
through the same task rebuilds the worker 10.2 s after the fire
(`scale worker 2` at 71.2 s, fire at 61.0 s), reports it at 77.4 s
(detection latency 16.4 s, precision 1.0, recall 1.0) and scores TR 0.996
(960/964), recovered at 30.5 s. `run_table.py` with mockllm on
`entity_destruction,belt_cut` x seed 3 on two slots finishes in 3.5 min
with no error rows: TR 0.625 (floor-adj 0.250) and 0.110, both censored in
the Kaplan-Meier pool.
