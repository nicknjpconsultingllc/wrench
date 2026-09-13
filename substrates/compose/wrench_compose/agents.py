"""The agent surface as the sandbox sees it (`wrenchctl` run inside the agent
container) and the scripted fixture agents: noop, restart_all, oracle.

Fixtures never touch an admin path: every action is `docker exec <sandbox>
wrenchctl --json ...`, so a passing bracket proves the agent-visible surface
is sufficient to recover from every kind."""

import json
import subprocess
import threading
import time


class AgentError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.payload = message


class Wrenchctl:
    """Runs wrenchctl inside the sandbox container and parses its --json output."""

    def __init__(self, container: str):
        self.container = container

    def _run(self, *args: str, timeout: float = 180) -> object:
        r = subprocess.run(
            ["docker", "exec", self.container, "wrenchctl", "--json", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = None
        if r.stdout.strip():
            try:
                payload = json.loads(r.stdout)
            except json.JSONDecodeError:
                payload = r.stdout
        if r.returncode != 0:
            msg = payload.get("error") if isinstance(payload, dict) else None
            raise AgentError(msg or r.stderr.strip() or f"wrenchctl exited {r.returncode}")
        return payload

    def ps(self):
        return self._run("ps")

    def logs(self, service: str, tail: int = 50):
        return self._run("logs", service, "--tail", str(tail))

    def exec(self, service: str, cmd: list[str]):
        return self._run("exec", service, "--", *cmd)

    def restart(self, service: str):
        return self._run("restart", service)

    def scale(self, service: str, replicas: int):
        return self._run("scale", service, str(replicas))

    def config(self, service: str, env: dict):
        return self._run("config", "edit", service, *[f"{k}={v}" for k, v in env.items()])

    def metrics(self):
        return self._run("metrics")

    def report_fault(self, service: str, cause: str):
        return self._run("report_fault", service, cause)


class Agent:
    name = "base"

    def __init__(self, client: Wrenchctl, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.stop = threading.Event()
        self.actions: list[dict] = []

    def on_fire(self, fired: dict) -> None:
        """Called once, right after the fire is observed by the runner."""

    def finish(self) -> None:
        self.stop.set()

    def _act(self, label, fn, *a):
        t = time.monotonic()
        try:
            res = fn(*a)
            self.actions.append({"action": label, "args": a, "ok": True, "ms": int((time.monotonic() - t) * 1000)})
            return res
        except (AgentError, subprocess.TimeoutExpired) as e:
            self.actions.append({"action": label, "args": a, "ok": False, "error": str(e)})
            return None


class Noop(Agent):
    name = "noop"


class RestartAll(Agent):
    """Restarts every factory service every `period_s`, starting at the fire.
    Must fail the floor test: victims were rm'd, toxics and hogs persist."""

    name = "restart_all"
    period_s = 15

    def on_fire(self, fired: dict) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            for svc in ("gateway", "worker", "redis", "postgres"):
                if self.stop.is_set():
                    return
                self._act("restart", self.client.restart, svc)
            self.stop.wait(self.period_s)


class Oracle(Agent):
    """Knows the fault from the manifest and repairs it immediately, using
    only wrenchctl from inside the sandbox."""

    name = "oracle"

    def on_fire(self, fired: dict) -> None:
        kind = fired["kind"]
        affected = fired["affected"]
        if kind in ("entity_destruction", "adaptive_strike"):
            svc = affected[0]["service"]
            self._act("report_fault", self.client.report_fault, svc, f"{affected[0]['container']} is gone")
            want = self.cfg["workers"] if svc == "worker" else 1
            self._act("scale", self.client.scale, svc, want)
        elif kind == "belt_cut":
            self._act(
                "report_fault", self.client.report_fault, "worker", "workers cannot reach the queue through netproxy"
            )
            self._act("config", self.client.config, "worker", {"REDIS_URL": "redis://redis:6379/0"})
        elif kind == "resource_exhaustion":
            self._act(
                "report_fault", self.client.report_fault, "postgres", "connection slots exhausted by role analytics"
            )
            self._act(
                "exec",
                self.client.exec,
                "postgres",
                [
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "factory",
                    "-c",
                    (
                        "ALTER ROLE analytics CONNECTION LIMIT 0; "
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='analytics';"
                    ),
                ],
            )


AGENTS = {"noop": Noop, "restart_all": RestartAll, "oracle": Oracle}
