"""Precondition-gated arming scheduler: a Python mirror of server.lua's
on_nth_tick loop. Pure; the probe feeds it samples and a `fire` callback.

State machine per spec: pending (chained, waits for predecessor) -> waiting
(needs trailing rate >= quota * fraction for `consecutive_windows` consecutive
samples) -> armed (fires at tick >= fire_at) -> resolved.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

MS_PER_MINUTE = 60000


@dataclass
class Spec:
    id: int
    kind: str
    seed: int
    params: dict = field(default_factory=dict)
    quota_item: str = "jobs_done"
    quota_per_min: float = 0.0
    quota_fraction: float = 1.0
    consecutive_windows: int = 2
    window_ms: int = 60000
    delay_ms: int = 0
    after_id: int | None = None
    state: str = "waiting"
    streak: int = 0
    fire_at: int = 0
    eligible_tick: int = 0


def trailing_rate(samples: list[dict], item: str, window_ms: int, min_tick: int = 0) -> float | None:
    """Items/min between the newest sample and the newest one at least
    window_ms older (ignoring samples before min_tick)."""
    n = len(samples)
    if n < 2:
        return None
    newest = samples[-1]
    if newest["tick"] < min_tick:
        return None
    base = None
    for i in range(n - 2, -1, -1):
        s = samples[i]
        if s["tick"] < min_tick:
            break
        if newest["tick"] - s["tick"] >= window_ms:
            base = s
            break
    if base is None:
        return None
    dt = newest["tick"] - base["tick"]
    if dt <= 0:
        return None
    produced = newest["counts"].get(item, 0) - base["counts"].get(item, 0)
    return produced * MS_PER_MINUTE / dt


# fire(spec) -> (manifest | None, error | None, design_avoided: bool)
FireFn = Callable[[Spec], tuple[list[dict] | None, str | None, bool]]


class Scheduler:
    def __init__(self, sample_cap: int = 100000):
        self.samples: list[dict] = []
        self.specs: dict[int, Spec] = {}
        self.resolved: dict[int, int] = {}
        self.events: list[dict] = []
        self.next_id = 1
        self.sample_cap = sample_cap

    def add_sample(self, tick: int, counts: dict) -> None:
        self.samples.append({"tick": tick, "counts": dict(counts)})
        if len(self.samples) > self.sample_cap:
            del self.samples[0]

    def arm(self, kind: str, seed: int, **kw) -> int:
        sid = self.next_id
        self.next_id += 1
        spec = Spec(id=sid, kind=kind, seed=seed, **kw)
        spec.state = "pending" if spec.after_id is not None else "waiting"
        self.specs[sid] = spec
        return sid

    def fire_now(self, sid: int) -> None:
        spec = self.specs[sid]
        spec.state = "armed"
        spec.fire_at = 0

    def step(self, tick: int, fire: FireFn) -> list[dict]:
        """Evaluate every spec at `tick`; returns the ledger events emitted."""
        emitted: list[dict] = []
        for sid, spec in list(self.specs.items()):
            if spec.state == "pending" and spec.after_id in self.resolved:
                spec.state = "waiting"
                spec.streak = 0
                spec.eligible_tick = self.resolved[spec.after_id]
            if spec.state == "waiting":
                rate = trailing_rate(self.samples, spec.quota_item, spec.window_ms, spec.eligible_tick)
                if rate is not None and rate >= spec.quota_per_min * spec.quota_fraction:
                    spec.streak += 1
                else:
                    spec.streak = 0
                if spec.streak >= spec.consecutive_windows:
                    spec.state = "armed"
                    spec.fire_at = tick + spec.delay_ms
                    emitted.append(self._event(tick, "armed", spec, detail={"id": sid, "rate_per_min": rate}))
            if spec.state == "armed" and tick >= spec.fire_at:
                manifest, err, design_avoided = fire(spec)
                if manifest is not None:
                    emitted.append(self._event(tick, "fired", spec, affected=manifest, detail={"id": sid}))
                elif design_avoided:
                    emitted.append(self._event(tick, "not_applicable", spec, detail={"id": sid, "error": err}))
                else:
                    emitted.append(self._event(tick, "failed", spec, detail={"id": sid, "error": err}))
                del self.specs[sid]
                self.resolved[sid] = tick
        self.events.extend(emitted)
        return emitted

    @staticmethod
    def _event(tick, event, spec, affected=None, detail=None) -> dict:
        return {
            "tick": tick,
            "event": event,
            "kind": spec.kind,
            "seed": spec.seed,
            "affected": list(affected or []),
            "detail": dict(detail or {}),
        }
