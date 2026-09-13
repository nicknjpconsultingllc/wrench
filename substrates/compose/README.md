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
make test            # 34 unit tests, no Docker
make test-partition  # 9 anti-gaming checks from inside the sandbox, ~1 min with Docker
make test-live       # every compose_live test: partition + bracketing + 4 no-op floor cells, ~25 min
make demo            # no-op vs oracle on entity_destruction, ~8 min
make jitter          # 10 no-op replays x 2 kinds + 3 oracle replays x 2 kinds
make floor           # 4 kinds x {noop, restart_all, oracle}
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
service's position, strict radius 3 matches only the right service.

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

- No LLM driver yet: `system_prompt` exists and the fixtures drive
  `wrenchctl` through `docker exec`, but nothing wires a model to the sandbox
  shell.
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
