"""The agent surface client (what `wrenchctl` wraps) and the scripted fixture
agents: noop, restart_all, oracle."""

import threading
import time

from wrench_compose.httpjson import HttpError, get, post


class AgentClient:
    """Python client for the allowlisted /agent/* API."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def ps(self):
        return get(self.base + "/agent/ps")

    def logs(self, service: str, tail: int = 50):
        return get(f"{self.base}/agent/logs?service={service}&tail={tail}")

    def exec(self, service: str, cmd):
        return post(self.base + "/agent/exec", {"service": service, "cmd": cmd}, timeout=60)

    def restart(self, service: str):
        return post(self.base + "/agent/restart", {"service": service}, timeout=60)

    def scale(self, service: str, replicas: int):
        return post(self.base + "/agent/scale", {"service": service, "replicas": replicas}, timeout=120)

    def config(self, service: str, env: dict):
        return post(self.base + "/agent/config", {"service": service, "env": env}, timeout=120)

    def metrics(self):
        return get(self.base + "/agent/metrics")

    def report_fault(self, service: str, cause: str):
        return post(self.base + "/agent/report_fault", {"service": service, "cause": cause})


class Agent:
    name = "base"

    def __init__(self, client: AgentClient, cfg: dict):
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
        except HttpError as e:
            self.actions.append({"action": label, "args": a, "ok": False, "error": str(e.payload)})
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
    only the agent surface."""

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
