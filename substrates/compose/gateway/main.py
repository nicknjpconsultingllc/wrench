"""Gateway: accepts POST /jobs {job_id, sig} and appends to the redis stream.
The single ingress; if it dies, jobs are lost (loadgen is open-loop)."""

import os
import threading
import time

import redis

from wrench_compose.httpjson import serve

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PORT = int(os.environ.get("PORT", "8000"))
STREAM = "jobs"

r = redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=1)
lock = threading.Lock()
stats = {"inflight": 0, "admitted": 0, "rejected": 0}


def post_jobs(_q, body):
    job_id, sig = body.get("job_id"), body.get("sig")
    if not job_id or not sig:
        return 400, {"error": "job_id and sig required"}
    with lock:
        stats["inflight"] += 1
    try:
        r.xadd(STREAM, {"job_id": job_id, "sig": sig, "t": str(time.time())})
        with lock:
            stats["admitted"] += 1
        return 202, {"ok": True}
    except redis.RedisError as e:
        with lock:
            stats["rejected"] += 1
        return 503, {"error": f"queue unavailable: {e}"}
    finally:
        with lock:
            stats["inflight"] -= 1


def metrics(_q, _b):
    with lock:
        return 200, dict(stats)


def health(_q, _b):
    return 200, {"ok": True}


if __name__ == "__main__":
    serve(PORT, {("POST", "/jobs"): post_jobs, ("GET", "/metrics"): metrics, ("GET", "/health"): health})
    print(f"gateway listening on :{PORT} -> {REDIS_URL}", flush=True)
    while True:
        time.sleep(3600)
