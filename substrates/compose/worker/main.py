"""Worker: consume one job at a time from the redis stream (consumer group
`workers`), burn a fixed amount of CPU, commit one row, ack.

Postgres connections are opened per job on purpose: that is what makes a
connection-slot hog (resource_exhaustion) bite. Redis errors reconnect; a
Postgres insert retries until it succeeds (the job stays in flight)."""

import hashlib
import os
import socket
import time

import psycopg
import redis

from wrench_compose.httpjson import serve

REDIS_URL = os.environ.get("REDIS_URL", "redis://netproxy:6379/0")
PG_DSN = os.environ.get("PG_DSN", "postgresql://worker:worker@postgres:5432/factory")
WORK_ITERS = int(os.environ.get("WORK_ITERS", "1000000"))
# "cputime" (default): spin until this process has consumed WORK_CPU_MS of
# CPU time (fixed CPU time; wall time independent of core speed, only of
# contention). "iters": a fixed number of sha256 iterations (fixed work; wall
# time follows core speed) -- see docs/jitter_study.md for why the default
# is cputime.
WORK_MODE = os.environ.get("WORK_MODE", "cputime")
WORK_CPU_MS = int(os.environ.get("WORK_CPU_MS", "195"))
STREAM, GROUP = "jobs", "workers"
CONSUMER = socket.gethostname()
AUTOCLAIM_EVERY = 10  # loops; stale PEL entries of dead workers get re-run
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8000"))
stats = {"done": 0, "started_at": time.time()}  # read by the agent API's `metrics`


def do_work(iters: int) -> str:
    h = b"0"
    if WORK_MODE == "cputime":
        budget = WORK_CPU_MS / 1000.0
        start = time.process_time()
        while time.process_time() - start < budget:
            for _ in range(2000):
                h = hashlib.sha256(h).digest()
        return h.hex()[:8]
    for _ in range(iters):
        h = hashlib.sha256(h).digest()
    return h.hex()[:8]


def connect_redis():
    return redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=2, decode_responses=True)


def ensure_group(r):
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def commit(job_id: str, sig: str) -> None:
    delay = 0.2
    while True:
        try:
            with psycopg.connect(PG_DSN, connect_timeout=3) as conn:
                conn.execute(
                    "INSERT INTO jobs_done (job_id, sig, worker) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (job_id, sig, CONSUMER),
                )
            return
        except psycopg.Error as e:
            print(f"pg insert failed ({type(e).__name__}: {str(e).strip()[:80]}); retrying", flush=True)
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)


def main():
    print(f"worker {CONSUMER} redis={REDIS_URL} mode={WORK_MODE} iters={WORK_ITERS} cpu_ms={WORK_CPU_MS}", flush=True)
    serve(METRICS_PORT, {("GET", "/metrics"): lambda _q, _b: (200, {"consumer": CONSUMER, **stats})})
    r = None
    loops = 0
    done = 0
    while True:
        try:
            if r is None:
                r = connect_redis()
                ensure_group(r)
                print(f"connected to {REDIS_URL}", flush=True)
            loops += 1
            entries = []
            if loops % AUTOCLAIM_EVERY == 0:
                _, claimed, *_ = r.xautoclaim(STREAM, GROUP, CONSUMER, min_idle_time=15000, start_id="0-0", count=1)
                entries = list(claimed)
            if not entries:
                res = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=1, block=1000)
                entries = res[0][1] if res else []
            for mid, fields in entries:
                do_work(WORK_ITERS)
                commit(fields["job_id"], fields["sig"])
                r.xack(STREAM, GROUP, mid)
                done += 1
                stats["done"] = done
                if done % 50 == 0:
                    print(f"done={done}", flush=True)
        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                ensure_group(r)
            else:
                print(f"redis error: {e}", flush=True)
                r = None
                time.sleep(0.5)
        except (redis.RedisError, OSError) as e:
            print(f"redis unavailable: {type(e).__name__}: {e}", flush=True)
            r = None
            time.sleep(0.5)


if __name__ == "__main__":
    main()
