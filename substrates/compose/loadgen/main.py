"""Open-loop, seeded load generator: one job every 1/RPS seconds on a
monotonic schedule regardless of what the gateway does. Job ids derive from
the seed; each is stamped with an HMAC the factory never has the key for.
Failed sends are lost (never retried) -- that is what open-loop means."""

import http.client
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from wrench_compose.signing import read_secret, sign

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
RPS = float(os.environ.get("RPS", "8"))
SEED = int(os.environ.get("SEED", "1"))
KEY = read_secret(os.environ.get("HMAC_KEY_FILE", "/run/secrets/hmac_key"))

u = urlparse(GATEWAY_URL)
NS = uuid.uuid5(uuid.NAMESPACE_URL, f"wrench-loadgen-{SEED}")
lock = threading.Lock()
stats = {"sent": 0, "ok": 0, "failed": 0}


def send(i: int):
    job_id = str(uuid.uuid5(NS, str(i)))
    body = json.dumps({"job_id": job_id, "sig": sign(KEY, job_id)})
    try:
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=2)
        c.request("POST", "/jobs", body=body, headers={"Content-Type": "application/json"})
        resp = c.getresponse()
        resp.read()
        c.close()
        ok = resp.status == 202
    except OSError:
        ok = False
    with lock:
        stats["ok" if ok else "failed"] += 1


def main():
    print(f"loadgen rps={RPS} seed={SEED} -> {GATEWAY_URL}", flush=True)
    pool = ThreadPoolExecutor(max_workers=64)
    t0 = time.monotonic()
    i = 0
    last_report = t0
    while True:
        due = t0 + i / RPS
        now = time.monotonic()
        if now < due:
            time.sleep(due - now)
        pool.submit(send, i)
        with lock:
            stats["sent"] += 1
        i += 1
        if now - last_report >= 10:
            last_report = now
            with lock:
                print(json.dumps({"t": round(now - t0, 1), **stats}), flush=True)


if __name__ == "__main__":
    main()
