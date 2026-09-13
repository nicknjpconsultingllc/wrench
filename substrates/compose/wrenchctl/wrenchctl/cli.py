"""wrenchctl <verb> ... : the agent's whole toolbox.

Every verb maps to one call on the agent API; the API decides what is
addressable (only the factory's own services) and enforces caps.
"""

import argparse
import json
import sys

from wrenchctl import api, render

DESCRIPTION = """Operate the job pipeline you are responsible for.

Services: gateway (HTTP ingress -> queue), redis (the queue), worker (queue
-> postgres, N replicas), postgres (committed jobs). Workers reach redis
through the `netproxy` hop.

Verbs: ps, logs, exec, restart, scale, config edit, metrics, report_fault.
`metrics` is the pipeline's own view: gateway counters, queue depth and per-
worker completions. Use `report_fault <service> <cause>` as soon as you
believe a service is the cause of a throughput drop."""

EPILOG = """examples:
  wrenchctl ps
  wrenchctl logs worker --tail 20
  wrenchctl exec postgres -- psql -U postgres -d factory -c 'select count(*) from jobs_done'
  wrenchctl restart gateway
  wrenchctl scale worker 3
  wrenchctl config edit worker REDIS_URL=redis://redis:6379/0
  wrenchctl metrics
  wrenchctl report_fault worker 'worker-1 is gone'"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wrenchctl",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="print the raw API response")
    sub = p.add_subparsers(dest="verb", required=True, metavar="verb")

    sub.add_parser("ps", help="list the pipeline's containers and their state")

    lg = sub.add_parser("logs", help="tail a service's container logs")
    lg.add_argument("service")
    lg.add_argument("--tail", type=int, default=50, help="lines per container (default 50)")

    ex = sub.add_parser(
        "exec", help="run a command inside a running container of a service: exec <service> -- <cmd...>"
    )
    ex.add_argument("service")
    ex.add_argument("cmd", nargs=argparse.REMAINDER, help="command after `--`")

    rs = sub.add_parser(
        "restart", help="restart every container of a service (a removed container cannot be restarted; use scale)"
    )
    rs.add_argument("service")

    sc = sub.add_parser(
        "scale", help="set a service's replica count (0..4); rebuilds missing containers from the service's definition"
    )
    sc.add_argument("service")
    sc.add_argument("replicas", type=int)

    cf = sub.add_parser(
        "config", help="config edit <service> KEY=VALUE ...: change environment and recreate the service"
    )
    cfs = cf.add_subparsers(dest="config_verb", required=True, metavar="edit")
    ed = cfs.add_parser("edit", help="set KEY=VALUE environment entries and recreate the service's containers")
    ed.add_argument("service")
    ed.add_argument("kv", nargs="+", metavar="KEY=VALUE")

    sub.add_parser("metrics", help="the pipeline's own throughput counters (gateway, queue, workers)")

    rf = sub.add_parser("report_fault", help="report which service you believe is faulty and why")
    rf.add_argument("service")
    rf.add_argument("cause", nargs="+", help="free text")
    return p


def run(argv: list[str] | None = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    try:
        if a.verb == "ps":
            payload = api.ps()
            text = render.ps(payload)
        elif a.verb == "logs":
            payload = api.logs(a.service, a.tail)
            text = render.logs(payload)
        elif a.verb == "exec":
            cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
            if not cmd:
                p.error("exec needs a command: exec <service> -- <cmd...>")
            payload = api.exec_(a.service, cmd)
            if a.json:
                print(json.dumps(payload, indent=2))
                return 0
            out, err, code = render.exec_(payload)
            sys.stdout.write(out)
            sys.stderr.write(err)
            return code
        elif a.verb == "restart":
            payload = api.restart(a.service)
            text = render.restart(payload)
        elif a.verb == "scale":
            payload = api.scale(a.service, a.replicas)
            text = render.scale(payload)
        elif a.verb == "config":
            env = {}
            for kv in a.kv:
                if "=" not in kv:
                    p.error(f"expected KEY=VALUE, got {kv!r}")
                k, v = kv.split("=", 1)
                env[k] = v
            payload = api.config_edit(a.service, env)
            text = render.config(payload)
        elif a.verb == "metrics":
            payload = api.metrics()
            text = render.metrics(payload)
        elif a.verb == "report_fault":
            payload = api.report_fault(a.service, " ".join(a.cause))
            text = render.report_fault(payload)
        else:  # pragma: no cover - argparse enforces the choices
            p.error(f"unknown verb {a.verb}")
    except api.ApiError as e:
        if a.json:
            print(json.dumps({"error": e.message, "status": e.status}))
        else:
            print(f"error: {e.message}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2) if a.json else text)
    return 0


def main() -> int:
    return run()
