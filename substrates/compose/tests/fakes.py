"""Test doubles for the LLM path: a fake stack (probe + compose) and a fake
sandbox, so ``ComposeEpisode`` runs its whole turn protocol without Docker."""

import subprocess

from wrench_compose.report import ITEM

SAMPLE_MS = 500


def synthetic_samples(end_tick: int, fire_tick: int | None = None, rate_before: float = 8.0, rate_after: float = 4.0):
    """Cumulative ``jobs_done`` samples every 500 ms: ``rate_before`` jobs/s
    until the fire, ``rate_after`` after it."""
    out = []
    total = 0.0
    for tick in range(0, end_tick + 1, SAMPLE_MS):
        rate = rate_before if fire_tick is None or tick <= fire_tick else rate_after
        out.append({"tick": tick, "counts": {ITEM: int(total)}})
        total += rate * SAMPLE_MS / 1000.0
    return out


PS_TEXT = (
    "SERVICE   CONTAINER                    STATE    STARTED\n"
    "gateway   wrench-factory-0-gateway-1   running  2026-09-12T10:00:00\n"
    "postgres  wrench-factory-0-postgres-1  running  2026-09-12T10:00:00\n"
    "redis     wrench-factory-0-redis-1     running  2026-09-12T10:00:00\n"
    "worker    wrench-factory-0-worker-1    running  2026-09-12T10:00:00\n"
    "worker    wrench-factory-0-worker-2    running  2026-09-12T10:00:00"
)
METRICS_TEXT = (
    "uptime_s          95\n"
    "gateway.admitted  760   (per min, trailing 30 s: 480.0)\n"
    "gateway.rejected  0\n"
    "gateway.inflight  1\n"
    "queue.length      760\n"
    "queue.pending     2\n"
    "queue.lag         0\n"
    "jobs_done         759   (per min, trailing 30 s: 478.0)\n"
    "  wrench-factory-0-worker-1: done=380\n"
    "  wrench-factory-0-worker-2: done=379"
)


class FakeStack:
    """Scripted probe: the tick advances ``tick_step`` ms per drain (one
    ``samples()`` read), the fire lands at ``fire_tick``. Records every call."""

    instances = []

    def __init__(self, slot, cfg, secrets_dir, *, fire_tick=60000, tick_step=1000, kind="entity_destruction"):
        self.slot = slot
        self.cfg = cfg
        self.secrets_dir = secrets_dir
        self.timing = {}
        self.calls = []
        self._tick = 0
        self.tick_step = tick_step
        self.fire_tick = fire_tick
        self.kind = kind
        self.seed = None
        self.reports = []
        self.up_called = False
        self.down_called = 0
        FakeStack.instances.append(self)

    def up(self):
        self.up_called = True
        self.calls.append("up")

    def arm(self, kind, seed, params=None):
        self.kind = kind
        self.seed = seed
        self.calls.append(("arm", kind, seed))
        return 1

    def status(self):
        return {"tick": self._tick, "resolved": [self._fire()] if self._fire() else []}

    def tick(self):
        return self.status()["tick"]

    def _fire(self):
        if self._tick < self.fire_tick:
            return None
        return {
            "tick": self.fire_tick,
            "event": "fired",
            "kind": self.kind,
            "seed": self.seed,
            "affected": [{"service": "worker", "container": "wrench-factory-0-worker-1", "x": 40.0, "y": 0.0}],
            "detail": {},
        }

    def resolved(self):
        return self._fire()

    def wait_resolved(self, timeout_s):
        self.calls.append("wait_resolved")
        self._tick = max(self._tick, self.fire_tick)
        return self._fire()

    def wait_tick(self, end_tick):
        self.calls.append(("wait_tick", end_tick))
        self._tick = max(self._tick, end_tick)

    def samples(self):
        self._tick += self.tick_step
        return synthetic_samples(self._tick, self.fire_tick if self._fire() else None)

    def ledger(self):
        events = []
        if self._tick >= 1000:
            events.append({"tick": 1000, "event": "armed", "kind": self.kind, "seed": self.seed})
        fire = self._fire()
        if fire:
            events.append(fire)
        events.extend(self.reports)
        return events

    def report(self, tick, service):
        from wrench_compose.positions import position_of

        x, y = position_of(service)
        self.reports.append(
            {
                "tick": tick,
                "event": "report_fault",
                "affected": [{"service": service, "x": x, "y": y}],
                "detail": {"service": service, "cause": "test", "x": x, "y": y},
            }
        )

    def dump(self, run_dir, samples, ledger):
        self.calls.append("dump")
        return {"tick": self._tick, "samples": len(samples), "jobs_done": 0, "rejected": 0, "pg_errors": 0}

    def down(self):
        self.down_called += 1
        self.calls.append("down")


class FakeSandbox:
    """Answers ``wrenchctl ps`` / ``metrics`` with canned text and records
    every other command; ``outputs`` maps a command to (rc, stdout, stderr)."""

    def __init__(self, container, outputs=None):
        self.container = container
        self.commands = []
        self.outputs = outputs or {}

    def run(self, command, timeout_s=120):
        self.commands.append(command)
        rc, out, err = self.outputs.get(command, (0, f"ran: {command}\n", ""))
        return subprocess.CompletedProcess(["sh", "-c", command], rc, out, err)

    def wrenchctl(self, *args):
        verb = " ".join(args)
        return {"ps": PS_TEXT, "metrics": METRICS_TEXT}.get(verb, f"({verb})")
