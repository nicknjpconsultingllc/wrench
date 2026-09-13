"""Chaos: the only container holding the Docker socket.

Two HTTP listeners on two networks:
  ADMIN_BIND:8080  /chaos/*  -- fire faults (called by the probe), blueprint.
                               Admin network only; the factory has no route.
  AGENT_BIND:8081  /agent/*  -- the agent surface behind `wrenchctl`:
                               ps/logs/exec/restart/scale/config/metrics/
                               report_fault, allowlisted to containers labeled
                               wrench.role=factory in the factory compose
                               project. Factory network only.
Recreation (scale up, config edit, oracle repair) works from a blueprint
snapshotted from the running factory at startup, so a removed container can
be rebuilt without compose. `metrics` is the factory's own view (gateway
counters, queue depth, worker completions), never the probe's samples.
"""

import os
import shlex
import threading
import time
from collections import deque
from pathlib import Path

import docker
import docker.errors
import redis

from wrench_compose.allowlist import COMPOSE_PROJECT_LABEL, is_agent_visible, service_of
from wrench_compose.httpjson import HttpError, get, post, serve
from wrench_compose.pick import load_bearing, pick_adaptive, pick_seeded
from wrench_compose.positions import position_of

FACTORY_PROJECT = os.environ["FACTORY_PROJECT"]
FACTORY_NET = os.environ["FACTORY_NET"]
ADMIN_PROJECT = os.environ.get("ADMIN_PROJECT", "wrench-admin-0")
TOXIPROXY_URL = os.environ["TOXIPROXY_URL"]
PROBE_URL = os.environ.get("PROBE_URL", "http://probe:8080")
ADMIN_BIND = os.environ.get("ADMIN_BIND", "0.0.0.0")
AGENT_BIND = os.environ.get("AGENT_BIND", "0.0.0.0")
HOG_IMAGE = os.environ.get("HOG_IMAGE", "wrench-svc:local")
ANALYTICS_PASSWORD = (
    Path(os.environ.get("ANALYTICS_PASSWORD_FILE", "/run/secrets/analytics_password")).read_text().strip()
)
SCALE_CAP = int(os.environ.get("SCALE_CAP", "4"))
PROXY = "worker_redis"
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
WORKER_METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", "8000"))
METRICS_WINDOW_MS = 30000

client = docker.from_env()
T0 = time.monotonic()


# ---------------------------------------------------------------- inventory
def factory_containers(all_states: bool = True):
    out = []
    for c in client.containers.list(all=all_states, filters={"label": f"{COMPOSE_PROJECT_LABEL}={FACTORY_PROJECT}"}):
        if is_agent_visible(c.labels, FACTORY_PROJECT):
            out.append(c)
    return sorted(out, key=lambda c: c.name)


def running(service: str | None = None):
    return [
        c
        for c in factory_containers(all_states=False)
        if c.status == "running" and (service is None or service_of(c.labels) == service)
    ]


def by_service(service: str):
    return [c for c in factory_containers() if service_of(c.labels) == service]


def ledger(event: str, **fields):
    try:
        post(PROBE_URL + "/ledger", {"event": event, **fields}, timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"ledger append failed: {e}", flush=True)


# ---------------------------------------------------------------- blueprint
BLUEPRINT: dict[str, dict] = {}


def snapshot() -> dict:
    for c in factory_containers():
        svc = service_of(c.labels)
        if svc in BLUEPRINT:
            continue
        a = c.attrs
        nets = a["NetworkSettings"]["Networks"]
        net = nets.get(FACTORY_NET) or next(iter(nets.values()))
        BLUEPRINT[svc] = {
            "image": a["Config"]["Image"],
            "cmd": a["Config"]["Cmd"],
            "env": dict(kv.split("=", 1) for kv in a["Config"]["Env"] if "=" in kv),
            "labels": dict(a["Config"]["Labels"]),
            "nano_cpus": a["HostConfig"].get("NanoCpus") or None,
            "memory": a["HostConfig"].get("Memory") or None,
            "binds": a["HostConfig"].get("Binds") or None,
            "aliases": [x for x in (net.get("Aliases") or []) if not x.startswith(c.id[:12])] or [svc],
            "healthcheck": a["Config"].get("Healthcheck"),
        }
    return {k: {"image": v["image"], "env": v["env"], "aliases": v["aliases"]} for k, v in BLUEPRINT.items()}


def next_name(service: str) -> str:
    taken = {c.name for c in by_service(service)}
    n = 1
    while f"{FACTORY_PROJECT}-{service}-{n}" in taken:
        n += 1
    return f"{FACTORY_PROJECT}-{service}-{n}"


def create_from_blueprint(service: str, name: str | None = None):
    if service not in BLUEPRINT:
        snapshot()
    bp = BLUEPRINT.get(service)
    if bp is None:
        raise HttpError(404, f"no blueprint for service {service}")
    name = name or next_name(service)
    labels = dict(bp["labels"])
    labels["com.docker.compose.container-number"] = name.rsplit("-", 1)[-1]
    hc = client.api.create_host_config(
        nano_cpus=bp["nano_cpus"], mem_limit=bp["memory"], restart_policy={"Name": "no"}, binds=bp["binds"]
    )
    nc = client.api.create_networking_config({FACTORY_NET: client.api.create_endpoint_config(aliases=bp["aliases"])})
    kw = {}
    if bp["healthcheck"]:
        kw["healthcheck"] = bp["healthcheck"]
    resp = client.api.create_container(
        image=bp["image"],
        command=bp["cmd"],
        environment=bp["env"],
        labels=labels,
        name=name,
        host_config=hc,
        networking_config=nc,
        detach=True,
        **kw,
    )
    client.api.start(resp["Id"])
    return client.containers.get(resp["Id"])


def destroy(c) -> None:
    try:
        c.kill()
    except docker.errors.APIError:
        pass
    c.remove(force=True)


# ---------------------------------------------------------------- secrets
def set_analytics_password() -> bool:
    """Give the `analytics` role its per-run password. The init SQL creates the
    role without one, so the credential lives in this process (from a Docker
    secret) and in Postgres's own catalog, never in a checked-in file. Re-run
    before every hog launch: a recreated postgres starts from the init SQL."""
    pgs = running("postgres")
    if not pgs:
        return False
    sql = f"ALTER ROLE analytics PASSWORD '{ANALYTICS_PASSWORD}'"
    res = pgs[0].exec_run(["psql", "-U", "postgres", "-d", "factory", "-qc", sql])
    if res.exit_code != 0:
        print(f"analytics password not set: {res.output[:200]!r}", flush=True)
        return False
    return True


def hog_dsn() -> str:
    return f"postgresql://analytics:{ANALYTICS_PASSWORD}@postgres:5432/factory"


# ---------------------------------------------------------------- faults
def manifest_entry(c, **extra) -> dict:
    svc = service_of(c.labels)
    x, y = position_of(svc)
    return {"service": svc, "container": c.name, "x": x, "y": y, **extra}


def fire_entity_destruction(seed: int, params: dict):
    candidates = params.get("candidates") or ["gateway", "worker"]
    pool = {c.name: c for c in running() if service_of(c.labels) in candidates}
    name = pick_seeded(seed, list(pool))
    if name is None:
        return None, "no candidate container running", True
    victim = pool[name]
    same_type_total = len(running(service_of(victim.labels)))
    entry = manifest_entry(victim, same_type_total=same_type_total)
    destroy(victim)
    return [entry], None, False


def fire_belt_cut(seed: int, params: dict):
    workers = running("worker")
    if not workers:
        return None, "no worker running", True
    mode = params.get("mode", "latency")
    if mode == "timeout":
        toxic = {
            "name": "belt_cut",
            "type": "timeout",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"timeout": 0},
        }
    else:
        toxic = {
            "name": "belt_cut",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": int(params.get("latency_ms", 1000)), "jitter": 0},
        }
    try:
        post(f"{TOXIPROXY_URL}/proxies/{PROXY}/toxics", toxic, timeout=5)
    except HttpError as e:
        return None, f"toxiproxy refused: {e.payload}", False
    entries = [
        manifest_entry(
            w, via="netproxy", toxic=toxic["type"], toxic_attributes=toxic["attributes"], same_type_total=len(workers)
        )
        for w in workers
    ]
    return entries, None, False


def fire_resource_exhaustion(seed: int, params: dict):
    pgs = running("postgres")
    if not pgs:
        return None, "postgres not running", True
    name = f"{ADMIN_PROJECT}-pghog"
    try:
        client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass
    if not set_analytics_password():
        return None, "could not set the analytics password on postgres", False
    hog = client.containers.run(
        HOG_IMAGE,
        command="python -m pghog.main",
        name=name,
        detach=True,
        network=FACTORY_NET,
        environment={"HOG_DSN": hog_dsn()},
        labels={"wrench.role": "admin", COMPOSE_PROJECT_LABEL: ADMIN_PROJECT, "com.docker.compose.service": "pghog"},
        restart_policy={"Name": "no"},
    )
    held = 0
    for _ in range(50):
        logs = hog.logs(tail=1).decode(errors="replace").strip()
        if logs.startswith("holding="):
            held = int(logs.split("=")[1])
            if held > 0:
                break
        time.sleep(0.1)
    return [manifest_entry(pgs[0], hog=name, hog_connections=held, same_type_total=len(pgs))], None, False


def fire_adaptive_strike(seed: int, params: dict):
    """Kill the holder whose loss removes the most capacity: window
    flow-through share / replica count (wrench_compose.pick.load_bearing).
    Holders: gateway, redis, each worker. A single point of failure scores
    1.0; one of n workers scores ~1/n^2. Ties (gateway vs redis) by seed."""
    flow = {k: float(v) for k, v in (params.get("flow") or {}).items()}
    live = running()
    resolved: dict[str, object] = {}
    for key in flow:
        if key.startswith("worker:"):
            host = key.split(":", 1)[1]
            match = [c for c in live if c.attrs["Config"]["Hostname"] == host and service_of(c.labels) == "worker"]
        else:
            match = [c for c in live if service_of(c.labels) == key]
        if match:
            resolved[key] = match[0]
    replicas = {svc: len(running(svc)) for svc in {k.split(":", 1)[0] for k in resolved}}
    scores = load_bearing({k: v for k, v in flow.items() if k in resolved}, replicas)
    key = pick_adaptive(scores, seed)
    if key is None:
        return None, "no holder carried any flow in the window", True
    victim = resolved[key]
    entry = manifest_entry(
        victim,
        holder=key,
        load_bearing=scores[key],
        scores=scores,
        flow=flow,
        inflight_all=params.get("inflight"),
        same_type_total=replicas[service_of(victim.labels)],
    )
    destroy(victim)
    return [entry], None, False


KINDS = {
    "entity_destruction": fire_entity_destruction,
    "belt_cut": fire_belt_cut,
    "resource_exhaustion": fire_resource_exhaustion,
    "adaptive_strike": fire_adaptive_strike,
}


def chaos_fire(_q, body):
    kind = body.get("kind")
    if kind not in KINDS:
        raise HttpError(400, f"unknown kind {kind}")
    snapshot()
    manifest, err, design_avoided = KINDS[kind](int(body.get("seed", 0)), body.get("params") or {})
    if manifest is None:
        return 409, {"error": err, "not_applicable": design_avoided}
    print(f"fired {kind}: {manifest}", flush=True)
    return 200, {"affected": manifest}


def chaos_blueprint(_q, _b):
    return 200, snapshot()


# ---------------------------------------------------------------- agent surface
def need_service(body) -> str:
    svc = body.get("service")
    if not svc or svc not in (BLUEPRINT.keys() | {service_of(c.labels) for c in factory_containers()}):
        raise HttpError(404, f"unknown factory service {svc!r}")
    return svc


def agent_ps(_q, _b):
    return 200, [
        {
            "name": c.name,
            "service": service_of(c.labels),
            "state": c.status,
            "status": c.attrs["State"].get("Status"),
            "started_at": c.attrs["State"].get("StartedAt"),
        }
        for c in factory_containers()
    ]


def agent_logs(q, _b):
    svc = need_service(q)
    tail = int(q.get("tail", 50))
    return 200, {c.name: c.logs(tail=tail).decode(errors="replace") for c in by_service(svc)}


def agent_exec(_q, body):
    svc = need_service(body)
    live = running(svc)
    if not live:
        raise HttpError(409, f"no running container for {svc}")
    cmd = body.get("cmd")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    res = live[0].exec_run(cmd, demux=True)
    out, err = res.output
    ledger("agent_action", detail={"action": "exec", "service": svc, "cmd": cmd, "exit_code": res.exit_code})
    return 200, {
        "container": live[0].name,
        "exit_code": res.exit_code,
        "stdout": (out or b"")[:20000].decode(errors="replace"),
        "stderr": (err or b"")[:20000].decode(errors="replace"),
    }


def agent_restart(_q, body):
    svc = need_service(body)
    cs = by_service(svc)
    if not cs:
        raise HttpError(404, f"no container exists for {svc} (nothing to restart)")
    for c in cs:
        c.restart(timeout=5)
    ledger("agent_action", detail={"action": "restart", "service": svc, "containers": [c.name for c in cs]})
    return 200, {"restarted": [c.name for c in cs]}


def agent_scale(_q, body):
    svc = need_service(body)
    n = int(body.get("replicas", 1))
    if n < 0 or n > SCALE_CAP:
        raise HttpError(400, f"replicas must be 0..{SCALE_CAP}")
    current = sorted(by_service(svc), key=lambda c: c.name)
    created, removed = [], []
    for c in current:
        if c.status != "running":
            destroy(c)
            removed.append(c.name)
    current = [c for c in current if c.name not in removed]
    while len(current) > n:
        c = current.pop()
        destroy(c)
        removed.append(c.name)
    while len(current) < n:
        c = create_from_blueprint(svc)
        current.append(c)
        created.append(c.name)
    ledger(
        "agent_action",
        detail={"action": "scale", "service": svc, "replicas": n, "created": created, "removed": removed},
    )
    return 200, {"created": created, "removed": removed, "running": [c.name for c in current]}


def agent_config(_q, body):
    svc = need_service(body)
    env = body.get("env") or {}
    if not isinstance(env, dict) or not env:
        raise HttpError(400, "env must be a non-empty object")
    snapshot()
    BLUEPRINT[svc]["env"].update({str(k): str(v) for k, v in env.items()})
    recreated = []
    for c in sorted(by_service(svc), key=lambda c: c.name):
        name = c.name
        destroy(c)
        create_from_blueprint(svc, name=name)
        recreated.append(name)
    ledger("agent_action", detail={"action": "config", "service": svc, "env": env, "recreated": recreated})
    return 200, {"recreated": recreated, "env": BLUEPRINT[svc]["env"]}


# ---------------------------------------------------------------- factory-side metrics
class FactoryMetrics:
    """The agent-visible throughput view, sampled from the factory itself:
    gateway counters, redis stream depth, each worker's `done`. Counters are
    accumulated as deltas per container so a restarted or recreated
    container (counter back to 0) does not make the totals drop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last: dict[str, int] = {}
        self.admitted = 0
        self.done = 0
        self.window: deque[tuple[int, int, int]] = deque(maxlen=200)
        self.snapshot: dict = {}
        self.r = redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True)

    def _bump(self, key: str, value: int, field: str) -> None:
        prev = self.last.get(key, 0)
        delta = value - prev if value >= prev else value
        self.last[key] = value
        setattr(self, field, getattr(self, field) + max(0, delta))

    def sample(self) -> None:
        gw: dict = {}
        workers: dict = {}
        queue: dict = {}
        unreachable: list[str] = []
        try:
            m = get(GATEWAY_URL + "/metrics", timeout=0.5)
            gw = {"admitted": int(m["admitted"]), "rejected": int(m["rejected"]), "inflight": int(m["inflight"])}
        except Exception:  # noqa: BLE001 - gateway down is a fact worth showing, not an error
            unreachable.append("gateway")
        for c in running("worker"):
            ip = c.attrs["NetworkSettings"]["Networks"].get(FACTORY_NET, {}).get("IPAddress")
            try:
                m = get(f"http://{ip}:{WORKER_METRICS_PORT}/metrics", timeout=0.5)
                workers[c.name] = {"done": int(m["done"])}
            except Exception:  # noqa: BLE001
                workers[c.name] = {"done": None, "error": "unreachable"}
        try:
            queue["length"] = int(self.r.xlen("jobs"))
            for g in self.r.xinfo_groups("jobs"):
                if g["name"] == "workers":
                    queue["pending"] = int(g.get("pending") or 0)
                    queue["lag"] = int(g.get("lag") or 0)
        except Exception:  # noqa: BLE001
            unreachable.append("redis")
        with self.lock:
            if gw:
                self._bump("gateway", gw["admitted"], "admitted")
            for name, w in workers.items():
                if w["done"] is not None:
                    self._bump(name, w["done"], "done")
            t = int((time.monotonic() - T0) * 1000)
            self.window.append((t, self.admitted, self.done))
            gw["admitted_per_min"] = self._rate(1)
            self.snapshot = {
                "uptime_s": t // 1000,
                "gateway": gw,
                "queue": queue,
                "workers": workers,
                "jobs_done": self.done,
                "jobs_done_per_min": self._rate(2),
                "unreachable": unreachable,
            }

    def _rate(self, idx: int) -> float | None:
        if len(self.window) < 2:
            return None
        newest = self.window[-1]
        base = None
        for s in reversed(self.window):
            if newest[0] - s[0] >= METRICS_WINDOW_MS:
                base = s
                break
        if base is None:
            return None
        dt = newest[0] - base[0]
        return round((newest[idx] - base[idx]) * 60000 / dt, 1) if dt > 0 else None

    def loop(self):
        while True:
            try:
                self.sample()
            except Exception as e:  # noqa: BLE001
                print(f"metrics sample failed: {e}", flush=True)
            time.sleep(1.0)

    def current(self) -> dict:
        with self.lock:
            return dict(self.snapshot)


METRICS = FactoryMetrics()


def agent_metrics(_q, _b):
    return 200, METRICS.current()


def agent_report_fault(_q, body):
    svc = body.get("service")
    if not svc:
        raise HttpError(400, "service required")
    entry = post(
        PROBE_URL + "/ledger",
        {"event": "report_fault", "detail": {"service": svc, "cause": str(body.get("cause", ""))}},
        timeout=5,
    )
    return 200, {"recorded": True, "tick": entry["tick"]}


ADMIN_ROUTES = {
    ("POST", "/chaos/fire"): chaos_fire,
    ("POST", "/chaos/blueprint"): chaos_blueprint,
}
AGENT_ROUTES = {
    ("GET", "/agent/ps"): agent_ps,
    ("GET", "/agent/logs"): agent_logs,
    ("POST", "/agent/exec"): agent_exec,
    ("POST", "/agent/restart"): agent_restart,
    ("POST", "/agent/scale"): agent_scale,
    ("POST", "/agent/config"): agent_config,
    ("GET", "/agent/metrics"): agent_metrics,
    ("POST", "/agent/report_fault"): agent_report_fault,
}


def main():
    for _ in range(60):  # the factory is `up --wait`ed before admin comes up; this only covers a manual start
        if snapshot():
            break
        time.sleep(1)
    set_analytics_password()
    threading.Thread(target=METRICS.loop, daemon=True).start()
    serve(8080, ADMIN_ROUTES, host=ADMIN_BIND)
    serve(8081, AGENT_ROUTES, host=AGENT_BIND)
    print(
        f"chaos up; admin {ADMIN_BIND}:8080 agent {AGENT_BIND}:8081; factory project={FACTORY_PROJECT} "
        f"net={FACTORY_NET} blueprint={sorted(BLUEPRINT)} docker={client.api.version()['Version']}",
        flush=True,
    )
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
