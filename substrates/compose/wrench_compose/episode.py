"""Episode runner: generate the run's secrets, bring up both compose projects,
arm the spec, wait for the fire, run a fixture agent through the sandbox,
run to window end, tear down, write samples.jsonl + ledger.jsonl +
episode.json under runs/<name>/."""

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from wrench_compose.agents import AGENTS, AgentError, Wrenchctl
from wrench_compose.ledger import write_jsonl
from wrench_compose.report import score_run

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose"


class EpisodeError(RuntimeError):
    pass


def _sh(args, env, check=True, capture=False, timeout=300):
    return subprocess.run(args, env=env, check=check, capture_output=capture, text=True, timeout=timeout)


def compose(env, file, *args, **kw):
    return _sh(["docker", "compose", "-f", str(COMPOSE / file), *args], env, **kw)


def slot_env(slot: int, cfg: dict, secrets_dir: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "WRENCH_SLOT": str(slot),
            "WRENCH_FACTORY_PROJECT": f"wrench-factory-{slot}",
            "WRENCH_ADMIN_PROJECT": f"wrench-admin-{slot}",
            "WRENCH_FACTORY_NET": f"wrench_factory_{slot}",
            "WRENCH_ADMIN_NET": f"wrench_admin_{slot}",
            "WRENCH_SECRETS_DIR": str(secrets_dir),
            "WRENCH_WORKERS": str(cfg["workers"]),
            "WRENCH_RPS": str(cfg["rps"]),
            "WRENCH_SEED": str(cfg["seed"]),
            "WRENCH_WORK_ITERS": str(cfg["work_iters"]),
            "WRENCH_SAMPLE_MS": str(cfg["sample_ms"]),
            "WRENCH_WORK_MODE": cfg.get("work_mode", "cputime"),
            "WRENCH_WORK_CPU_MS": str(cfg.get("work_cpu_ms", 195)),
            "WRENCH_IMAGE": cfg.get("image", "wrench-svc:local"),
            "WRENCH_AGENT_IMAGE": cfg.get("agent_image", "wrench-agent:local"),
        }
    )
    return env


def write_secrets(secrets_dir: Path) -> None:
    """Per-run secrets, mounted as Docker secrets into admin containers only:
    the HMAC key (loadgen signs, probe verifies) and the analytics password
    (chaos sets it on postgres and hands it to the hog). Removed at teardown."""
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    for name, value in (("hmac.key", secrets.token_hex(32)), ("analytics.pw", secrets.token_urlsafe(24))):
        path = secrets_dir / name
        path.write_text(value + "\n")
        os.chmod(path, 0o600)


def agent_container(slot: int) -> str:
    return f"wrench-factory-{slot}-agent-1"


def wait_agent_surface(ctl: Wrenchctl, timeout_s: float = 60) -> list:
    """Until `wrenchctl ps` answers from inside the sandbox (chaos has
    snapshotted the blueprint and opened the agent listener)."""
    t = time.monotonic()
    last = None
    while time.monotonic() - t < timeout_s:
        try:
            ps = ctl.ps()
            if ps:
                return ps
        except (AgentError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            last = e
        time.sleep(0.5)
    raise EpisodeError(f"agent surface not reachable from the sandbox after {timeout_s}s: {last}")


def teardown(env, slot: int, secrets_dir: Path | None = None):
    subprocess.run(["docker", "rm", "-f", f"wrench-admin-{slot}-pghog"], env=env, capture_output=True, check=False)
    compose(env, "admin.yml", "down", "-v", "--remove-orphans", "-t", "3", check=False, capture=True)
    compose(env, "factory.yml", "down", "-v", "--remove-orphans", "-t", "3", check=False, capture=True)
    if secrets_dir is not None:
        shutil.rmtree(secrets_dir, ignore_errors=True)


class ProbeClient:
    """Host-side access to the probe. Nothing on the admin side publishes a
    port (a published port is reachable from every bridge on the host), so
    each call is `docker exec <probe> python -m wrench_compose.httpjson`."""

    def __init__(self, slot: int):
        self.container = f"wrench-admin-{slot}-probe-1"
        self.base = f"http://10.231.{slot}.11:8080"

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30.0):
        argv = ["docker", "exec", self.container, "python", "-m", "wrench_compose.httpjson", method, self.base + path]
        if body is not None:
            argv.append(json.dumps(body))
        argv.append(str(timeout))
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15)
        if r.returncode != 0 or not r.stdout.strip():
            raise EpisodeError(
                f"probe {method} {path}: exit {r.returncode} {r.stdout.strip()[:300]} {r.stderr.strip()[-300:]}"
            )
        return json.loads(r.stdout)

    def get(self, path: str, timeout: float = 30.0):
        return self.request("GET", path, None, timeout)

    def post(self, path: str, body: dict | None = None, timeout: float = 30.0):
        return self.request("POST", path, body or {}, timeout)

    def wait(self, timeout_s: float = 60):
        t = time.monotonic()
        last = None
        while time.monotonic() - t < timeout_s:
            try:
                return self.get("/status", timeout=2)
            except (EpisodeError, subprocess.TimeoutExpired) as e:
                last = e
                time.sleep(0.5)
        raise EpisodeError(f"probe not reachable after {timeout_s}s: {last}")


def run_episode(
    name: str,
    kind: str,
    seed: int,
    agent_name: str,
    *,
    params: dict | None = None,
    window_ms: int = 120000,
    quota_per_min: float = 400.0,
    quota_fraction: float = 1.0,
    consecutive_windows: int = 2,
    precondition_window_ms: int = 60000,
    workers: int = 2,
    rps: float = 8.0,
    work_iters: int = 1000000,
    work_mode: str = "cputime",
    work_cpu_ms: int = 195,
    image: str = "wrench-svc:local",
    agent_image: str = "wrench-agent:local",
    sample_ms: int = 500,
    slot: int = 0,
    fire_timeout_s: float = 240,
    out_root: Path | None = None,
    label: str = "",
) -> dict:
    out_root = out_root or (ROOT / "runs")
    run_dir = out_root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": name,
        "kind": kind,
        "seed": seed,
        "agent": agent_name,
        "params": params or {},
        "window_ms": window_ms,
        "quota_per_min": quota_per_min,
        "quota_fraction": quota_fraction,
        "consecutive_windows": consecutive_windows,
        "precondition_window_ms": precondition_window_ms,
        "workers": workers,
        "rps": rps,
        "work_iters": work_iters,
        "work_mode": work_mode,
        "work_cpu_ms": work_cpu_ms,
        "image": image,
        "agent_image": agent_image,
        "sample_ms": sample_ms,
        "slot": slot,
        "label": label,
    }
    secrets_dir = run_dir / ".secrets"
    env = slot_env(slot, cfg, secrets_dir)
    probe = ProbeClient(slot)
    ctl = Wrenchctl(agent_container(slot))
    timing = {"t_start": time.time()}
    result = {"config": dict(cfg), "timing": timing, "error": None}
    teardown(env, slot)
    try:
        write_secrets(secrets_dir)
        t = time.monotonic()
        compose(env, "factory.yml", "up", "-d", "--wait", "--quiet-pull", capture=True)
        timing["factory_up_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        compose(env, "admin.yml", "up", "-d", "--quiet-pull", capture=True)
        probe.wait()
        wait_agent_surface(ctl)
        timing["admin_up_s"] = round(time.monotonic() - t, 1)
        armed = probe.post(
            "/arm",
            {
                "kind": kind,
                "seed": seed,
                "params": params or {},
                "quota_item": "jobs_done",
                "quota_per_min": quota_per_min,
                "quota_fraction": quota_fraction,
                "consecutive_windows": consecutive_windows,
                "window_ms": precondition_window_ms,
            },
        )
        result["spec_id"] = armed["id"]

        # wait for the fire (or a not_applicable / failed resolution)
        t = time.monotonic()
        fired = None
        while time.monotonic() - t < fire_timeout_s:
            st = probe.get("/status", timeout=5)
            if st["resolved"]:
                fired = st["resolved"][0]
                break
            time.sleep(0.5)
        if fired is None:
            raise EpisodeError(f"no fire within {fire_timeout_s}s; last status {st}")
        timing["fire_wall_s"] = round(time.monotonic() - t, 1)
        result["fired"] = fired
        agent = AGENTS[agent_name](ctl, cfg)
        if fired["event"] == "fired":
            t = time.monotonic()
            agent.on_fire(fired)
            timing["agent_on_fire_s"] = round(time.monotonic() - t, 2)
            end_tick = fired["tick"] + window_ms + 3 * sample_ms
            while probe.get("/status", timeout=5)["tick"] < end_tick:
                time.sleep(1)
        agent.finish()
        result["agent_actions"] = agent.actions

        samples = probe.get("/samples", timeout=30)
        ledger = probe.get("/ledger", timeout=30)
        with (run_dir / "samples.jsonl").open("w") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
        write_jsonl(run_dir / "ledger.jsonl", ledger)
        status = probe.get("/status", timeout=5)
        result["probe_status"] = {k: status[k] for k in ("tick", "samples", "jobs_done", "rejected", "pg_errors")}
        logs = compose(env, "admin.yml", "logs", "--no-color", "--tail", "400", check=False, capture=True).stdout
        (run_dir / "admin_logs.txt").write_text(logs)
        flogs = compose(env, "factory.yml", "logs", "--no-color", "--tail", "200", check=False, capture=True).stdout
        (run_dir / "factory_logs.txt").write_text(flogs)
        result["scores"] = score_run(run_dir, window_ms)
    except (subprocess.CalledProcessError, EpisodeError, Exception) as e:  # noqa: BLE001
        detail = getattr(e, "stderr", None) or ""
        result["error"] = f"{type(e).__name__}: {e} {detail[-2000:]}"
    finally:
        t = time.monotonic()
        teardown(env, slot, secrets_dir)
        timing["teardown_s"] = round(time.monotonic() - t, 1)
        timing["t_end"] = time.time()
        timing["wall_s"] = round(timing["t_end"] - timing["t_start"], 1)
        (run_dir / "episode.json").write_text(json.dumps(result, indent=2, default=str))
    return result
