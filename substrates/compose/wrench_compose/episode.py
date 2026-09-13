"""Episode runner: bring up both compose projects, arm the spec, wait for the
fire, run a fixture agent, run to window end, tear down, write
samples.jsonl + ledger.jsonl + episode.json under runs/<name>/."""

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from wrench_compose.agents import AGENTS, AgentClient
from wrench_compose.httpjson import get, post
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


def slot_env(slot: int, cfg: dict) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "WRENCH_SLOT": str(slot),
            "WRENCH_FACTORY_PROJECT": f"wrench-factory-{slot}",
            "WRENCH_ADMIN_PROJECT": f"wrench-admin-{slot}",
            "WRENCH_FACTORY_NET": f"wrench_factory_{slot}",
            "WRENCH_ADMIN_NET": f"wrench_admin_{slot}",
            "WRENCH_PROBE_PORT": str(9012 + 10 * slot),
            "WRENCH_CHAOS_PORT": str(9011 + 10 * slot),
            "WRENCH_WORKERS": str(cfg["workers"]),
            "WRENCH_RPS": str(cfg["rps"]),
            "WRENCH_SEED": str(cfg["seed"]),
            "WRENCH_HMAC_KEY": cfg["hmac_key"],
            "WRENCH_WORK_ITERS": str(cfg["work_iters"]),
            "WRENCH_SAMPLE_MS": str(cfg["sample_ms"]),
            "WRENCH_WORK_MODE": cfg.get("work_mode", "iters"),
            "WRENCH_WORK_CPU_MS": str(cfg.get("work_cpu_ms", 195)),
            "WRENCH_IMAGE": cfg.get("image", "wrench-svc:local"),
        }
    )
    return env


def teardown(env, slot: int):
    subprocess.run(["docker", "rm", "-f", f"wrench-admin-{slot}-pghog"], env=env, capture_output=True, check=False)
    compose(env, "admin.yml", "down", "-v", "--remove-orphans", "-t", "3", check=False, capture=True)
    compose(env, "factory.yml", "down", "-v", "--remove-orphans", "-t", "3", check=False, capture=True)


def wait_http(url: str, timeout_s: float = 60):
    t = time.monotonic()
    while time.monotonic() - t < timeout_s:
        try:
            return get(url, timeout=2)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise EpisodeError(f"{url} not reachable after {timeout_s}s")


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
    work_mode: str = "iters",
    work_cpu_ms: int = 195,
    image: str = "wrench-svc:local",
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
        "sample_ms": sample_ms,
        "slot": slot,
        "hmac_key": secrets.token_hex(16),
        "label": label,
    }
    env = slot_env(slot, cfg)
    probe = f"http://127.0.0.1:{env['WRENCH_PROBE_PORT']}"
    chaos = f"http://127.0.0.1:{env['WRENCH_CHAOS_PORT']}"
    timing = {"t_start": time.time()}
    result = {"config": {k: v for k, v in cfg.items() if k != "hmac_key"}, "timing": timing, "error": None}
    teardown(env, slot)
    try:
        t = time.monotonic()
        compose(env, "factory.yml", "up", "-d", "--wait", "--quiet-pull", capture=True)
        timing["factory_up_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        compose(env, "admin.yml", "up", "-d", "--quiet-pull", capture=True)
        wait_http(probe + "/status")
        wait_http(chaos + "/agent/ps")
        post(chaos + "/chaos/blueprint")
        timing["admin_up_s"] = round(time.monotonic() - t, 1)
        armed = post(
            probe + "/arm",
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
            st = get(probe + "/status", timeout=5)
            if st["resolved"]:
                fired = st["resolved"][0]
                break
            time.sleep(0.5)
        if fired is None:
            raise EpisodeError(f"no fire within {fire_timeout_s}s; last status {st}")
        timing["fire_wall_s"] = round(time.monotonic() - t, 1)
        result["fired"] = fired
        agent = AGENTS[agent_name](AgentClient(chaos), cfg)
        if fired["event"] == "fired":
            t = time.monotonic()
            agent.on_fire(fired)
            timing["agent_on_fire_s"] = round(time.monotonic() - t, 2)
            end_tick = fired["tick"] + window_ms + 3 * sample_ms
            while get(probe + "/status", timeout=5)["tick"] < end_tick:
                time.sleep(1)
        agent.finish()
        result["agent_actions"] = agent.actions

        samples = get(probe + "/samples", timeout=30)
        ledger = get(probe + "/ledger", timeout=30)
        with (run_dir / "samples.jsonl").open("w") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
        write_jsonl(run_dir / "ledger.jsonl", ledger)
        status = get(probe + "/status", timeout=5)
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
        teardown(env, slot)
        timing["teardown_s"] = round(time.monotonic() - t, 1)
        timing["t_end"] = time.time()
        timing["wall_s"] = round(timing["t_end"] - timing["t_start"], 1)
        (run_dir / "episode.json").write_text(json.dumps(result, indent=2, default=str))
    return result
