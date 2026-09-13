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
```

Two compose projects on separate networks. `compose/factory.yml` is what the
agent sees (labels `wrench.role=factory`, `restart: "no"` everywhere, CPU and
memory limits, pinned image digests). `compose/admin.yml` is loadgen, probe,
chaos and Toxiproxy; loadgen/probe/Toxiproxy also attach to the factory
network because they must touch it, but Toxiproxy's control API binds only to
its static admin-network IP and the agent API allowlists on labels.

Throughput is HMAC-verified rows per minute: loadgen stamps every job id with
an HMAC whose key exists only in the admin project; the probe counts a row
only when the stamp verifies. Nothing inside the factory can mint throughput.

## Run

```bash
make venv            # uv venv + pip install -e .[dev]
make build           # one image, wrench-svc:local, for all Python services
make test            # 20 unit tests, no Docker
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

1. `factory.yml up --wait`, then `admin.yml up`. Tick 0 = probe start.
2. Runner arms one spec on the probe: quota 400 jobs/min, fraction 1.0, two
   consecutive 500 ms samples whose trailing 60 s rate meets it.
3. The probe fires through `chaos` and records `armed`/`fired` with the
   affected manifest (`service`, `container`, synthetic `x,y`,
   `same_type_total` = replicas of that service at fire time, kind extras).
4. Runner hands the fired event to the fixture agent, then waits until
   `tick >= fire_tick + 120 s` no matter what the agent does.
5. Both projects come down; files are written; the fork's scorers run.

Defaults: 8 jobs/s open-loop, 2 workers, 1,000,000 sha256 iterations per job
(~195 ms CPU + ~15 ms I/O in the container, so one worker caps near 300-370
jobs/min depending on which host core it lands on and misses the 400 quota;
two workers are load-limited at 480 = 1.2x quota). `WRENCH_WORK_MODE=cputime`
fixes CPU time per job (`WRENCH_WORK_CPU_MS`, default 195) instead of
iterations; see the jitter study for when that matters.

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

## Agent surface

`chaos` exposes `/agent/*`; `wrench_compose.agents.AgentClient` wraps it:
`ps`, `logs`, `exec`, `restart`, `scale` (capped at 4), `config` (env edit +
recreate), `metrics` (trailing jobs/min), `report_fault <service> <cause>`.
Only containers labeled `wrench.role=factory` in the factory project are
addressable (`wrench_compose/allowlist.py`). Every mutating call also appends
an `agent_action` ledger event.

Fixture agents (`wrench_compose/agents.py`): `noop`, `restart_all` (restarts
every factory service every 15 s from the fire), `oracle` (reads the fired
manifest and applies the repair in the table above).

## What is stubbed

- The sandbox container and the `wrenchctl` binary. The agent surface is the
  HTTP API plus the Python client, called from the host. The allowlist is real.
- `chaos` is reachable from the host on `127.0.0.1:9011` and serves both
  `/chaos/*` and `/agent/*` on one port; a real sandbox would see only
  `/agent/*` through a proxy or a token.
- Toxiproxy's control port is bound to the admin IP, but `netproxy:6379` is
  on the factory network by design (it is the belt).
- The `analytics` password is in the factory's Postgres init script; treat it
  as a leaked reporting credential.
- One `pghog` per fire; a second `resource_exhaustion` in the same episode
  replaces it.
- `restart_all`'s restart of `redis` drops the stream (no persistence) and of
  `postgres` recreates nothing (data survives in the container filesystem).

## Results

`docs/jitter_study.md`: 10 no-op replays per kind give TR CV 0.045
(`entity_destruction`, worker victim) and 0.004 (`belt_cut`), baseline
480.5 +/- 0.9 jobs/min, fire at 60.8 +/- 0.26 s; oracle 0.999 +/- 0.001. Floor
and bracketing for all four kinds are in the same file, with the raw runs
under `runs/`.
