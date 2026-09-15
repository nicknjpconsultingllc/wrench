"""Fired-event manifest entries (``affected``), shared by chaos and the tests.

Every entry carries the service's synthetic position so ``wrench_core``'s
position matcher can credit a ``report_fault`` on it (strict radius 3,
services 20 units apart: only the named service matches).
"""

from wrench_compose.positions import position_of

# A belt_cut degrades the worker->redis hop inside the Toxiproxy `netproxy`;
# the agent sees it as workers stalling on redis. Reports naming the proxy
# or redis are as right as reports naming a worker.
BELT_CUT_PATH = ("netproxy", "redis")


def service_entry(service: str, container: str | None = None, **extra) -> dict:
    x, y = position_of(service)
    return {"service": service, "container": container, "x": x, "y": y, **extra}


def belt_cut_affected(worker_entries: list[dict], toxic: dict) -> list[dict]:
    """The worker entries first (``affected[0]`` stays a worker, as before),
    then the path services the toxic sits on."""
    return list(worker_entries) + [
        service_entry(svc, via="netproxy", toxic=toxic["type"], toxic_attributes=toxic["attributes"])
        for svc in BELT_CUT_PATH
    ]


# A silent_throttle installs an AFTER INSERT trigger on `jobs_done` that sleeps
# on every commit, so the fault lives entirely inside `postgres`. Unlike
# belt_cut there is no ambiguous hop: the workers are victims, not the cause,
# so only `postgres` is in the manifest and a report naming the (obviously
# slow) worker is a precision miss. Nothing is destroyed, so `same_type_total`
# is informational and the kind stays out of ``redundancy_kinds``.
SILENT_THROTTLE_SERVICE = "postgres"


def silent_throttle_affected(container: str | None, delay_s: float, same_type_total: int) -> list[dict]:
    return [
        service_entry(
            SILENT_THROTTLE_SERVICE,
            container,
            mechanism="commit_trigger_delay",
            delay_s=delay_s,
            same_type_total=same_type_total,
        )
    ]
