"""Probe: the benchmark's clock, sampler, arming scheduler and ledger.

Every SAMPLE_MS it counts HMAC-verified committed rows over its own Postgres
connection and appends {"tick": ms_since_start, "counts": {"jobs_done": n}}.
The scheduler (wrench_compose.arming) arms specs on the precondition and
fires them through `chaos`. Ledger events (armed/fired/report_fault/
agent_action) all get their tick from this clock. Samples and the ledger are
held in memory and served over HTTP; the host runner writes the files.
"""

import os
import threading
import time
from collections import deque

import psycopg
import redis

from wrench_compose.arming import Scheduler, Spec, trailing_rate
from wrench_compose.httpjson import HttpError, get, post, serve
from wrench_compose.positions import position_of
from wrench_compose.signing import read_secret, verify

PG_DSN = os.environ["PG_DSN"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
KEY = read_secret(os.environ.get("HMAC_KEY_FILE", "/run/secrets/hmac_key"))
CHAOS_URL = os.environ.get("CHAOS_URL", "http://chaos:8080")
BIND = os.environ.get("BIND", "0.0.0.0")  # the admin-network address; the factory never sees this server
SAMPLE_MS = int(os.environ.get("SAMPLE_MS", "500"))
ITEM = os.environ.get("ITEM", "jobs_done")
INFLIGHT_WINDOW_SAMPLES = 20  # 10 s at 500 ms

T0 = time.monotonic()


def tick() -> int:
    return int((time.monotonic() - T0) * 1000)


class Counter:
    """Cumulative count of HMAC-verified rows, incrementally over `seq` with a
    200-row overlap so late-committing lower seqs are still picked up."""

    def __init__(self):
        self.conn = None
        self.seen: set[str] = set()
        self.max_seq = 0
        self.verified = 0
        self.rejected = 0
        self.errors = 0

    def _connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(PG_DSN, connect_timeout=2, autocommit=True)
        return self.conn

    def refresh(self) -> bool:
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT seq, job_id, sig FROM jobs_done WHERE seq > %s ORDER BY seq",
                (max(0, self.max_seq - 200),),
            ).fetchall()
        except psycopg.Error:
            self.errors += 1
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:  # noqa: BLE001, S110 - already handling the primary error
                pass
            self.conn = None
            return False
        for seq, job_id, sig in rows:
            if job_id in self.seen:
                continue
            self.seen.add(job_id)
            if verify(KEY, job_id, sig):
                self.verified += 1
            else:
                self.rejected += 1
            self.max_seq = max(self.max_seq, seq)
        return True

    def flow_window(self, seconds: int = 10) -> dict[str, float]:
        """Jobs committed in the trailing window, per holder they traversed:
        every job passed through gateway and redis; each worker gets its own
        rows. Postgres is deliberately absent (stateful; not a strike target)."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT worker, count(*) FROM jobs_done WHERE committed_at > now() - make_interval(secs => %s) GROUP BY worker",
            (seconds,),
        ).fetchall()
        total = float(sum(n for _, n in rows))
        out = {"gateway": total, "redis": total}
        for host, n in rows:
            out[f"worker:{host}"] = float(n)
        return out

    def active_worker_backends(self) -> int:
        conn = self._connect()
        return conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename='worker' AND state='active'"
        ).fetchone()[0]


class InflightSampler:
    """Per-holder in-flight work over a trailing window, for adaptive_strike."""

    def __init__(self, counter: Counter):
        self.counter = counter
        self.window: deque[dict] = deque(maxlen=INFLIGHT_WINDOW_SAMPLES)
        self.r = redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True)

    def sample(self) -> None:
        now: dict[str, float] = {}
        try:
            now["gateway"] = float(get(GATEWAY_URL + "/metrics", timeout=0.5)["inflight"])
        except Exception:  # noqa: BLE001, S110 - holder unreachable; leave it out of this sample
            pass
        try:
            for g in self.r.xinfo_groups("jobs"):
                if g["name"] == "workers":
                    now["redis"] = float(g.get("lag") or 0)
            for c in self.r.xinfo_consumers("jobs", "workers"):
                now[f"worker:{c['name']}"] = float(c["pending"])
        except Exception:  # noqa: BLE001, S110 - holder unreachable; leave it out of this sample
            pass
        try:
            now["postgres"] = float(self.counter.active_worker_backends())
        except Exception:  # noqa: BLE001, S110 - holder unreachable; leave it out of this sample
            pass
        self.window.append(now)

    def totals(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.window:
            for k, v in s.items():
                out[k] = out.get(k, 0.0) + v
        return out


class Probe:
    def __init__(self):
        self.lock = threading.Lock()
        self.sched = Scheduler()
        self.counter = Counter()
        self.inflight = InflightSampler(self.counter)
        self.ledger: list[dict] = []
        self.last_count = 0

    def _fire(self, spec: Spec):
        params = dict(spec.params)
        if spec.kind == "adaptive_strike":
            params["inflight"] = self.inflight.totals()
            try:
                params["flow"] = self.counter.flow_window()
            except Exception as e:  # noqa: BLE001
                params["flow_error"] = str(e)
        try:
            res = post(CHAOS_URL + "/chaos/fire", {"kind": spec.kind, "seed": spec.seed, "params": params}, timeout=90)
        except HttpError as e:
            payload = e.payload if isinstance(e.payload, dict) else {"error": str(e.payload)}
            return None, str(payload.get("error", payload)), bool(payload.get("not_applicable"))
        except Exception as e:  # noqa: BLE001
            return None, f"chaos unreachable: {e}", False
        return res["affected"], None, False

    def loop(self):
        n = 0
        while True:
            due = T0 + (n * SAMPLE_MS) / 1000.0
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            n += 1
            fresh = self.counter.refresh()
            t = tick()
            with self.lock:
                self.sched.add_sample(t, {ITEM: self.counter.verified})
                if not fresh:
                    self.sched.samples[-1]["stale"] = True
                needs_inflight = any(s.kind == "adaptive_strike" for s in self.sched.specs.values())
            if needs_inflight:
                self.inflight.sample()
            with self.lock:
                events = self.sched.step(t, self._fire)
                for ev in events:
                    self.ledger.append(ev)
                    print(f"[{t}] {ev['event']} {ev['kind']} {ev.get('affected')} {ev.get('detail')}", flush=True)

    def rate(self) -> float | None:
        with self.lock:
            return trailing_rate(self.sched.samples, ITEM, 60000)

    # ---- routes ----
    def status(self, _q, _b):
        with self.lock:
            specs = [
                {"id": s.id, "kind": s.kind, "state": s.state, "streak": s.streak} for s in self.sched.specs.values()
            ]
            resolved = [e for e in self.ledger if e["event"] in ("fired", "not_applicable", "failed")]
            n = len(self.sched.samples)
        return 200, {
            "tick": tick(),
            "samples": n,
            "jobs_done": self.counter.verified,
            "rejected": self.counter.rejected,
            "pg_errors": self.counter.errors,
            "rate_per_min": self.rate(),
            "specs": specs,
            "resolved": resolved,
        }

    def samples(self, _q, _b):
        with self.lock:
            return 200, list(self.sched.samples)

    def get_ledger(self, _q, _b):
        with self.lock:
            return 200, list(self.ledger)

    def arm(self, _q, body):
        allowed = {
            "params",
            "quota_item",
            "quota_per_min",
            "quota_fraction",
            "consecutive_windows",
            "window_ms",
            "delay_ms",
            "after_id",
        }
        kw = {k: v for k, v in body.items() if k in allowed}
        with self.lock:
            sid = self.sched.arm(body["kind"], int(body.get("seed", 0)), **kw)
        return 200, {"id": sid, "tick": tick()}

    def fire_now(self, _q, body):
        with self.lock:
            if int(body["id"]) not in self.sched.specs:
                raise HttpError(404, f"unknown id {body['id']}")
            self.sched.fire_now(int(body["id"]))
        return 200, {"id": body["id"], "tick": tick()}

    def append_ledger(self, _q, body):
        event = body.get("event")
        if not event:
            raise HttpError(400, "event required")
        entry = {
            "tick": tick(),
            "event": event,
            "kind": body.get("kind"),
            "seed": body.get("seed"),
            "affected": list(body.get("affected") or []),
            "detail": dict(body.get("detail") or {}),
        }
        if event == "report_fault":
            svc = entry["detail"].get("service")
            x, y = position_of(svc)
            entry["affected"] = [{"service": svc, "x": x, "y": y}]
            entry["detail"].update({"x": x, "y": y})
        with self.lock:
            self.ledger.append(entry)
        return 200, entry


def main():
    p = Probe()
    serve(
        8080,
        {
            ("GET", "/status"): p.status,
            ("GET", "/samples"): p.samples,
            ("GET", "/ledger"): p.get_ledger,
            ("POST", "/arm"): p.arm,
            ("POST", "/fire_now"): p.fire_now,
            ("POST", "/ledger"): p.append_ledger,
        },
        host=BIND,
    )
    print(f"probe up on {BIND}:8080; sampling every {SAMPLE_MS} ms", flush=True)
    p.loop()


if __name__ == "__main__":
    main()
