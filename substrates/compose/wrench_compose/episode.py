"""One compose-substrate episode, harness-agnostic.

Two drivers share the stack lifecycle in ``Stack`` (per-run secrets, both
compose projects up, arm the spec on the probe, poll for the fire, samples
and ledger, teardown):

- ``run_episode``: the fixture path (``noop`` / ``restart_all`` / ``oracle``
  agents from ``wrench_compose.agents``) behind ``python -m wrench_compose``.
- ``ComposeEpisode``: the LLM path, with the same turn protocol as the
  Factorio substrate's ``WrenchEpisode``::

      obs = episode.observe()       # "{last feedback} --- Turn k/N + ps + metrics"
      ...model turn...
      episode.step(command)         # one shell line in the sandbox -> feedback

  ``step(None)`` is the "reply had no command" turn (it still consumes a
  turn), ``skip_step`` / ``fail_step`` are the two model-side failure paths,
  ``is_done`` flips once the turn budget is spent (or, by default, once the
  post-fire measurement window has closed: nothing after it is scored), and
  ``finalize()`` runs to the window end no matter what the agent did. The
  episode never ends early on quota: the fault arms on demonstrated
  throughput, so a quota-met pipeline is exactly when it gets interesting.

The agent acts only through ``docker exec <sandbox> sh -c <command>``. The
observation is the agent-visible view (``wrenchctl ps`` and
``wrenchctl metrics`` rendered by the same ``wrenchctl`` the sandbox
ships); the probe's samples and the ledger are scorer-only ground truth and
never appear in an observation (``tests/test_prompt_leak.py``).
"""

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wrench_core.metrics import episode_metrics, fired_events
from wrench_core.scoring import recovery_potential, shaped_reward_delta

from wrench_compose.agents import AGENTS, AgentError, Wrenchctl
from wrench_compose.ledger import write_jsonl
from wrench_compose.prompt import system_prompt
from wrench_compose.report import ITEM, score_run
from wrench_compose.scoring import COMPOSE_CONFIG

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose"

DEFAULT_TURNS = 30
# Minimum spacing between two executed commands. 30 turns x 6 s spans the
# 60 s arming baseline plus the 120 s post-fire window even when the model
# answers instantly, so a scripted policy sees the fault inside its budget.
DEFAULT_TURN_PERIOD_S = 6.0
COMMAND_TIMEOUT_S = 120
OUTPUT_CAP_BYTES = 6000

INITIAL_FEEDBACK = "The pipeline is up and taking load. Check its state and keep it healthy."
NO_COMMAND_FEEDBACK = "Your reply contained no shell command. Reply with exactly one command line in a ```sh block."
NO_OUTPUT_FEEDBACK = (
    "Your reply produced no visible output (likely spent its full token budget on reasoning). "
    "Reply with a shorter, more direct ```sh block."
)


class EpisodeError(RuntimeError):
    pass


def _sh(args, env, check=True, capture=False, timeout=300):
    return subprocess.run(args, env=env, check=check, capture_output=capture, text=True, timeout=timeout)


def compose(env, file, *args, **kw):
    return _sh(["docker", "compose", "-f", str(COMPOSE / file), *args], env, **kw)


def default_config(**overrides) -> dict:
    """The knobs both drivers pass to ``slot_env`` and record in episode.json."""
    cfg = {
        "window_ms": 120000,
        "quota_per_min": 400.0,
        "quota_fraction": 1.0,
        "consecutive_windows": 2,
        "precondition_window_ms": 60000,
        "workers": 2,
        "rps": 8.0,
        "work_iters": 1000000,
        "work_mode": "cputime",
        "work_cpu_ms": 195,
        "image": "wrench-svc:local",
        "agent_image": "wrench-agent:local",
        "sample_ms": 500,
        "fire_timeout_s": 240.0,
    }
    cfg.update(overrides)
    return cfg


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


# --------------------------------------------------------------------------
# The stack: everything on the admin side of the boundary, one slot
# --------------------------------------------------------------------------


class Stack:
    """One slot's two compose projects plus the probe, from secrets to
    teardown. Both drivers go through it; nothing else touches compose."""

    def __init__(self, slot: int, cfg: dict, secrets_dir: Path):
        self.slot = slot
        self.cfg = cfg
        self.secrets_dir = secrets_dir
        self.env = slot_env(slot, cfg, secrets_dir)
        self.probe = ProbeClient(slot)
        self.ctl = Wrenchctl(agent_container(slot))
        self.timing: dict = {}
        self.spec_id: int | None = None

    def up(self) -> None:
        teardown(self.env, self.slot)
        write_secrets(self.secrets_dir)
        t = time.monotonic()
        compose(self.env, "factory.yml", "up", "-d", "--wait", "--quiet-pull", capture=True)
        self.timing["factory_up_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        compose(self.env, "admin.yml", "up", "-d", "--quiet-pull", capture=True)
        self.probe.wait()
        wait_agent_surface(self.ctl)
        self.timing["admin_up_s"] = round(time.monotonic() - t, 1)

    def arm(self, kind: str, seed: int, params: dict | None = None) -> int:
        cfg = self.cfg
        armed = self.probe.post(
            "/arm",
            {
                "kind": kind,
                "seed": seed,
                "params": params or {},
                "quota_item": ITEM,
                "quota_per_min": cfg["quota_per_min"],
                "quota_fraction": cfg["quota_fraction"],
                "consecutive_windows": cfg["consecutive_windows"],
                "window_ms": cfg["precondition_window_ms"],
            },
        )
        self.spec_id = armed["id"]
        return self.spec_id

    def status(self) -> dict:
        return self.probe.get("/status", timeout=5)

    def tick(self) -> int:
        return int(self.status()["tick"])

    def resolved(self) -> dict | None:
        """The first fired / not_applicable / failed event, or None."""
        st = self.status()
        return st["resolved"][0] if st["resolved"] else None

    def wait_resolved(self, timeout_s: float) -> dict:
        t = time.monotonic()
        st = None
        while time.monotonic() - t < timeout_s:
            st = self.status()
            if st["resolved"]:
                return st["resolved"][0]
            time.sleep(0.5)
        raise EpisodeError(f"no fire within {timeout_s}s; last status {st}")

    def wait_tick(self, end_tick: int) -> None:
        while self.tick() < end_tick:
            time.sleep(1)

    def samples(self) -> list:
        return self.probe.get("/samples", timeout=30)

    def ledger(self) -> list:
        return self.probe.get("/ledger", timeout=30)

    def dump(self, run_dir: Path, samples: list, ledger: list) -> dict:
        """samples.jsonl, ledger.jsonl and the compose logs; returns the
        probe's closing status."""
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "samples.jsonl").open("w") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
        write_jsonl(run_dir / "ledger.jsonl", ledger)
        status = self.status()
        logs = compose(self.env, "admin.yml", "logs", "--no-color", "--tail", "400", check=False, capture=True).stdout
        (run_dir / "admin_logs.txt").write_text(logs)
        flogs = compose(
            self.env, "factory.yml", "logs", "--no-color", "--tail", "200", check=False, capture=True
        ).stdout
        (run_dir / "factory_logs.txt").write_text(flogs)
        return {k: status[k] for k in ("tick", "samples", "jobs_done", "rejected", "pg_errors")}

    def down(self) -> None:
        t = time.monotonic()
        teardown(self.env, self.slot, self.secrets_dir)
        self.timing["teardown_s"] = round(time.monotonic() - t, 1)


# --------------------------------------------------------------------------
# The fixture path
# --------------------------------------------------------------------------


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
    cfg = default_config(
        name=name,
        kind=kind,
        seed=seed,
        agent=agent_name,
        params=params or {},
        window_ms=window_ms,
        quota_per_min=quota_per_min,
        quota_fraction=quota_fraction,
        consecutive_windows=consecutive_windows,
        precondition_window_ms=precondition_window_ms,
        workers=workers,
        rps=rps,
        work_iters=work_iters,
        work_mode=work_mode,
        work_cpu_ms=work_cpu_ms,
        image=image,
        agent_image=agent_image,
        sample_ms=sample_ms,
        slot=slot,
        fire_timeout_s=fire_timeout_s,
        label=label,
    )
    stack = Stack(slot, cfg, run_dir / ".secrets")
    timing = stack.timing
    timing["t_start"] = time.time()
    result = {"config": dict(cfg), "timing": timing, "error": None}
    try:
        stack.up()
        result["spec_id"] = stack.arm(kind, seed, params)
        t = time.monotonic()
        fired = stack.wait_resolved(fire_timeout_s)
        timing["fire_wall_s"] = round(time.monotonic() - t, 1)
        result["fired"] = fired
        agent = AGENTS[agent_name](stack.ctl, cfg)
        if fired["event"] == "fired":
            t = time.monotonic()
            agent.on_fire(fired)
            timing["agent_on_fire_s"] = round(time.monotonic() - t, 2)
            stack.wait_tick(fired["tick"] + window_ms + 3 * sample_ms)
        agent.finish()
        result["agent_actions"] = agent.actions
        result["probe_status"] = stack.dump(run_dir, stack.samples(), stack.ledger())
        result["scores"] = score_run(run_dir, window_ms)
    except (subprocess.CalledProcessError, EpisodeError, Exception) as e:  # noqa: BLE001
        detail = getattr(e, "stderr", None) or ""
        result["error"] = f"{type(e).__name__}: {e} {detail[-2000:]}"
    finally:
        stack.down()
        timing["t_end"] = time.time()
        timing["wall_s"] = round(timing["t_end"] - timing["t_start"], 1)
        (run_dir / "episode.json").write_text(json.dumps(result, indent=2, default=str))
    return result


# --------------------------------------------------------------------------
# The LLM path: sandbox shell + turn protocol
# --------------------------------------------------------------------------


class Sandbox:
    """The agent's shell: ``docker exec`` into the sandbox container. The
    only way any driver runs anything on the agent's behalf."""

    def __init__(self, container: str):
        self.container = container

    def run(self, command: str, timeout_s: float = COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess:
        # coreutils `timeout` inside the container so a hung command dies
        # there too, not just the docker client on the host.
        argv = ["docker", "exec", self.container, "timeout", "-k", "5", str(int(timeout_s)), "sh", "-c", command]
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s + 15)
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(argv, 124, e.stdout or "", (e.stderr or "") + "\n(timed out)")

    def wrenchctl(self, *args: str) -> str:
        """Rendered (human) output of one wrenchctl verb, or its error text."""
        r = self.run("wrenchctl " + " ".join(args), timeout_s=30)
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"(wrenchctl {' '.join(args)} failed: {r.stderr.strip() or f'exit {r.returncode}'})"
        return out


_FENCE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```", re.DOTALL)


def parse_command(text: str | None) -> str | None:
    """Extract the one shell command line from a model reply, or None.

    The first fenced block wins (its first non-empty, non-comment line, a
    leading ``$ `` stripped). Without a fence: the first line that starts
    with ``wrenchctl``, else a single-line reply taken whole. Prose-only
    multi-line replies yield None (the no-command turn).
    """
    if not text or not text.strip():
        return None
    for block in _FENCE.findall(text):
        line = _first_command_line(block)
        if line:
            return line
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for ln in lines:
        candidate = ln[2:].strip() if ln.startswith("$ ") else ln
        if candidate.startswith("wrenchctl"):
            return candidate
    if len(lines) == 1 and not lines[0].startswith("```"):
        return lines[0][2:].strip() if lines[0].startswith("$ ") else lines[0]
    return None


def _first_command_line(block: str) -> str | None:
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        return ln[2:].strip() if ln.startswith("$ ") else ln
    return None


def cap_bytes(text: str, cap: int = OUTPUT_CAP_BYTES) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= cap:
        return text
    return raw[:cap].decode("utf-8", errors="ignore") + f"\n... [truncated {len(raw) - cap} bytes]"


def format_feedback(turn_index: int, command: str, exit_code: int, stdout: str, stderr: str, ms: int) -> str:
    out = cap_bytes(stdout.rstrip()) or "(empty)"
    err = cap_bytes(stderr.rstrip())
    text = (
        f"## Turn {turn_index + 1} result\n\n$ {command}\nexit code {exit_code} ({ms} ms)\n\nstdout:\n```\n{out}\n```"
    )
    if err:
        text += f"\nstderr:\n```\n{err}\n```"
    return text


def format_observation(feedback: str, turn_index: int, turns: int, ps: str, metrics: str) -> str:
    return (
        f"{feedback}\n\n---\n\n"
        f"## Turn {turn_index + 1}/{turns}\n\n"
        f"`wrenchctl ps`:\n```\n{ps}\n```\n\n"
        f"`wrenchctl metrics`:\n```\n{metrics}\n```\n\n"
        "Reply with ONE shell command line in a ```sh block for your next action."
    )


class ComposeEpisode:
    """One fault-kind episode against one compose slot.

    Construction is cheap and offline. ``start()`` brings the stack up and
    arms the spec; the caller owns slot allocation (``wrench_compose.slots``)
    and must call ``cleanup()`` in a ``finally``. ``finalize()`` waits for
    the fire and the end of the post-fire window, so an episode whose
    agent used its turns in the first minute is scored on the same window
    as one that acted throughout.

    ``stack_factory`` / ``sandbox_factory`` exist for the unit tests (no
    Docker); the defaults are ``Stack`` and ``Sandbox``.
    """

    def __init__(
        self,
        kind: str,
        seed: int = 1,
        *,
        slot: int = 0,
        turns: int = DEFAULT_TURNS,
        turn_period_s: float = DEFAULT_TURN_PERIOD_S,
        stop_after_window: bool = True,
        params: dict | None = None,
        out_root: Path | None = None,
        name: str | None = None,
        stack_factory: Callable[..., Any] = Stack,
        sandbox_factory: Callable[[str], Any] = Sandbox,
        **config_overrides,
    ):
        self.kind = kind
        self.seed = int(seed)
        self.slot = int(slot)
        self.turns = int(turns)
        self.turn_period_s = float(turn_period_s)
        self.stop_after_window = bool(stop_after_window)
        self.params = params or {}
        self.name = name or f"{kind}_seed{self.seed}_{uuid.uuid4().hex[:10]}"
        self.out_root = Path(out_root) if out_root else (ROOT / "runs")
        self.run_dir = self.out_root / self.name
        self.cfg = default_config(
            name=self.name,
            kind=kind,
            seed=self.seed,
            agent="llm",
            params=self.params,
            slot=self.slot,
            **config_overrides,
        )
        self.window_ms = int(self.cfg["window_ms"])
        self.quota_item = ITEM
        self.quota = float(self.cfg["quota_per_min"])
        self._stack_factory = stack_factory
        self._sandbox_factory = sandbox_factory
        self.stack = None
        self.sandbox = None

        self.samples: list[dict] = []
        self.ledger_events: list[dict] = []
        self.shaped_rewards: list[dict] = []
        self._shaped_fire_tick: int | None = None
        self._shaped_prev_tick: int | None = None
        self.actions: list[dict] = []

        self.turn = 0  # index of the upcoming turn, 0-based
        self.steps_completed = 0  # turns whose command actually ran
        self.quota_met = False
        self.fired: dict | None = None
        self.end_tick = 0
        self.error = ""
        self.feedback = INITIAL_FEEDBACK
        self._last_exec = 0.0
        self._result: dict[str, Any] | None = None
        self._closed = False
        self.timing: dict = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ComposeEpisode":
        self.timing["t_start"] = time.time()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.stack = self._stack_factory(self.slot, self.cfg, self.run_dir / ".secrets")
        self.stack.up()
        self.stack.arm(self.kind, self.seed, self.params)
        self.sandbox = self._sandbox_factory(agent_container(self.slot))
        return self

    @property
    def started(self) -> bool:
        return self.stack is not None

    @property
    def window_end_tick(self) -> int | None:
        if self.fired is None:
            return None
        return int(self.fired["tick"]) + self.window_ms + 3 * int(self.cfg["sample_ms"])

    @property
    def window_closed(self) -> bool:
        return self.fired is not None and bool(self.samples) and self.samples[-1]["tick"] >= self.window_end_tick

    @property
    def is_done(self) -> bool:
        if self.turn >= self.turns:
            return True
        return self.stop_after_window and self.window_closed

    def system_prompt(self) -> str:
        return system_prompt(workers=int(self.cfg["workers"]), turns=self.turns, quota=int(self.quota))

    # -- turn protocol -----------------------------------------------------

    def observe(self) -> str:
        """Observation for the upcoming turn: last feedback + the agent's own
        view of the pipeline. Never the probe's samples or the ledger."""
        self._require_started()
        ps = self.sandbox.wrenchctl("ps")
        metrics = self.sandbox.wrenchctl("metrics")
        return format_observation(self.feedback, self.turn, self.turns, ps, metrics)

    def step(self, command: str | None) -> str:
        """Run one shell command line in the sandbox (or the no-command
        turn), drain, return the feedback ``observe()`` will prepend."""
        self._require_started()
        if self.is_done:
            raise RuntimeError("episode is over: turn budget spent")
        turn = self.turn
        if not command:
            self._pace()
            self.feedback = NO_COMMAND_FEEDBACK
            self.actions.append({"turn": turn, "command": None})
            self.drain()
            self.turn += 1
            return self.feedback

        self._pace()
        t = time.monotonic()
        r = self.sandbox.run(command)
        ms = int((time.monotonic() - t) * 1000)
        self._last_exec = time.monotonic()
        self.steps_completed += 1
        self.feedback = format_feedback(turn, command, r.returncode, r.stdout or "", r.stderr or "", ms)
        self.actions.append(
            {
                "turn": turn,
                "command": command,
                "exit_code": r.returncode,
                "ms": ms,
                "stdout": cap_bytes(r.stdout or ""),
                "stderr": cap_bytes(r.stderr or ""),
            }
        )
        self.drain()
        logger.info(
            f"WRENCH compose {self.kind} turn {turn + 1}/{self.turns}: exit={r.returncode} "
            f"fired={self.fired is not None} samples={len(self.samples)} events={len(self.ledger_events)}"
        )
        self.turn += 1
        return self.feedback

    def skip_step(self, feedback: str) -> str:
        """Consume a turn on which no command could be attempted (the model
        call failed or produced no output). Still drains, still paced."""
        self._require_started()
        if self.is_done:
            raise RuntimeError("episode is over: turn budget spent")
        self._pace()
        self.feedback = feedback
        self.actions.append({"turn": self.turn, "command": None, "skipped": feedback})
        self.drain()
        self.turn += 1
        return self.feedback

    def fail_step(self, feedback: str) -> str:
        """Consume a turn that died in the driver (no drain)."""
        self.feedback = feedback
        self.actions.append({"turn": self.turn, "command": None, "failed": feedback})
        self.turn += 1
        return self.feedback

    def _pace(self) -> None:
        """Hold every turn to ``turn_period_s``, the no-command and no-output
        turns included: a model that answers with nothing must not burn its
        30-turn budget in the first seconds, before the fault has fired."""
        wait = self._last_exec + self.turn_period_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_exec = time.monotonic()

    # -- probe bookkeeping -------------------------------------------------

    def drain(self) -> dict | None:
        """Refresh samples, ledger and the fire from the probe; returns this
        drain's shaped-reward entry (or None). Never raises."""
        try:
            self.samples = self.stack.samples()
            self.ledger_events = self.stack.ledger()
            if self.fired is None:
                self.fired = self.stack.resolved()
            self.quota_met = self.quota_met or any(e.get("event") in ("armed", "fired") for e in self.ledger_events)
            return self._update_shaped_reward()
        except Exception as drain_err:  # noqa: BLE001
            logger.warning(f"WRENCH compose drain failed: {drain_err}")
            return None

    def _update_shaped_reward(self) -> dict | None:
        if not self.samples:
            return None
        fires = fired_events(self.ledger_events)
        if not fires:
            return None
        fire_tick = int(fires[-1].get("tick", 0))
        tick = self.samples[-1]["tick"]
        if fire_tick != self._shaped_fire_tick:
            self._shaped_fire_tick = fire_tick
            self._shaped_prev_tick = fire_tick
        delta = shaped_reward_delta(
            self.samples, self.quota_item, self._shaped_fire_tick, self._shaped_prev_tick, tick, config=COMPOSE_CONFIG
        )
        phi = recovery_potential(self.samples, self.quota_item, self._shaped_fire_tick, tick, config=COMPOSE_CONFIG)
        self._shaped_prev_tick = tick
        entry = {"tick": tick, "fire_tick": self._shaped_fire_tick, "phi": phi, "delta": delta}
        self.shaped_rewards.append(entry)
        return entry

    # -- end of episode ----------------------------------------------------

    @property
    def fires(self) -> list[dict]:
        return fired_events(self.ledger_events)

    def run_to_window_end(self) -> None:
        """Wait for the fire (the spec arms on demonstrated throughput) and
        then for the post-fire window to close. Raises ``EpisodeError``
        when nothing fires within the fire timeout: an episode with no
        fire is a broken episode, not a good agent."""
        self._require_started()
        if self.fired is None:
            self.fired = self.stack.wait_resolved(float(self.cfg["fire_timeout_s"]))
        if self.fired["event"] == "fired":
            self.stack.wait_tick(self.window_end_tick)

    def finalize(self) -> dict[str, Any]:
        """Run to the window end, final drain, compute every metric, write
        the run directory. Idempotent."""
        if self._result is not None:
            return self._result
        if self.started:
            try:
                self.run_to_window_end()
            finally:
                self.drain()
            try:
                self.end_tick = self.stack.tick()
            except Exception as tick_err:  # noqa: BLE001
                logger.warning(f"could not read the final tick: {tick_err}")
                if self.samples:
                    self.end_tick = int(self.samples[-1]["tick"])
            if self.fired is not None and self.fired["event"] == "fired":
                # The contract is a fixed post-fire window; turns the agent
                # took after it closed are not scored.
                self.end_tick = min(self.end_tick, int(self.fired["tick"]) + self.window_ms)
            try:
                self.timing["probe_status"] = self.stack.dump(self.run_dir, self.samples, self.ledger_events)
            except Exception as dump_err:  # noqa: BLE001
                logger.warning(f"could not write the run directory: {dump_err}")
        metrics = episode_metrics(
            self.samples, self.ledger_events, self.quota_item, self.end_tick, config=COMPOSE_CONFIG
        )
        self._result = {
            "kind": self.kind,
            "seed": self.seed,
            "slot": self.slot,
            "name": self.name,
            "turns": self.turns,
            "steps_taken": self.turn,
            "steps_completed": self.steps_completed,
            "quota_met": self.quota_met,
            "quota_item": self.quota_item,
            "quota": self.quota,
            "window_ms": self.window_ms,
            "end_tick": self.end_tick,
            "error": self.error,
            "samples": list(self.samples),
            "ledger_events": list(self.ledger_events),
            "shaped_rewards": list(self.shaped_rewards),
            "actions": list(self.actions),
            "fires": [{"kind": f.get("kind"), "tick": f.get("tick"), "seed": f.get("seed")} for f in self.fires],
            "num_fires": len(self.fires),
            "metrics": metrics["scalars"],
            "scores": {
                "throughput_retained": metrics["throughput_retained"],
                "recovery": metrics["recovery"],
                "detection": metrics["detection"],
                "time_to_recovery": metrics["time_to_recovery"],
            },
        }
        self._write_episode_json()
        return self._result

    def summary(self) -> str:
        return (
            f"Completed WRENCH compose episode {self.kind} (seed={self.seed}): "
            f"{self.steps_completed} commands run, quota_met={self.quota_met}, "
            f"{len(self.fires)} fault(s) fired, {len(self.samples)} samples."
        )

    def cleanup(self) -> None:
        """Tear both projects down and delete the secrets. Idempotent; never
        raises. Does not release the slot; the allocator does."""
        if self._closed:
            return
        self._closed = True
        if self.stack is not None:
            try:
                self.stack.down()
            except Exception as down_err:  # noqa: BLE001
                logger.error(f"compose teardown failed: {down_err}")
        self.timing["t_end"] = time.time()
        if "t_start" in self.timing:
            self.timing["wall_s"] = round(self.timing["t_end"] - self.timing["t_start"], 1)
        self._write_episode_json()

    def _write_episode_json(self) -> None:
        if not self.started:
            return
        try:
            record = {
                "config": dict(self.cfg),
                "timing": {**self.stack.timing, **self.timing},
                "error": self.error or None,
                "fired": self.fired,
                "agent_actions": self.actions,
                "scores": (self._result or {}).get("scores"),
                "metrics": (self._result or {}).get("metrics"),
            }
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "episode.json").write_text(json.dumps(record, indent=2, default=str))
        except Exception as write_err:  # noqa: BLE001
            logger.warning(f"could not write episode.json: {write_err}")

    def _require_started(self) -> None:
        if not self.started:
            raise RuntimeError("ComposeEpisode.start() has not been called")
