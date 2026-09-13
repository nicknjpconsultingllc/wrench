"""Connection-slot hog for resource_exhaustion: greedily opens Postgres
connections as the `analytics` role until the server refuses, holds them, and
re-grabs any slot it loses (so restarting Postgres does not clear it). It
stops only when the role can no longer connect (CONNECTION LIMIT 0) or the
container is removed."""

import os
import time

import psycopg

DSN = os.environ.get("HOG_DSN", "postgresql://analytics:analytics@postgres:5432/factory")


def main():
    held: list = []
    last = -1
    while True:
        alive = []
        for c in held:
            try:
                c.execute("SELECT 1")
                alive.append(c)
            except psycopg.Error:
                pass
        held = alive
        while True:
            try:
                held.append(psycopg.connect(DSN, connect_timeout=2, autocommit=True))
            except psycopg.Error:
                break
        if len(held) != last:
            print(f"holding={len(held)}", flush=True)
            last = len(held)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
