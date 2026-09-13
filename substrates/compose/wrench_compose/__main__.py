"""CLI: python -m wrench_compose <run|demo|jitter|floor|score> ..."""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from wrench_compose.episode import ROOT, run_episode
from wrench_compose.report import one_line, score_run

KINDS = ["entity_destruction", "belt_cut", "resource_exhaustion", "adaptive_strike"]
# Seeds chosen against the default 2-worker topology (candidates sorted:
# gateway-1, worker-1, worker-2): seed 3 -> worker (redundant, TR~0.6 no-op),
# seed 1 -> gateway (SPOF, floor test).
SEED_WORKER = 3
SEED_GATEWAY = 1


def _run(name, kind, seed, agent, **kw):
    print(f"--- {name}: {kind} seed={seed} agent={agent}", flush=True)
    t = time.monotonic()
    r = run_episode(name, kind, seed, agent, **kw)
    if r["error"]:
        print(f"    ERROR {r['error'][:400]}", flush=True)
    else:
        print("    " + one_line(name, r["scores"]), flush=True)
    print(
        f"    wall {r['timing']['wall_s']}s (up {r['timing'].get('factory_up_s')}+{r['timing'].get('admin_up_s')}s, "
        f"fire after {r['timing'].get('fire_wall_s')}s, teardown {r['timing'].get('teardown_s')}s) "
        f"[{time.monotonic() - t:.0f}s]",
        flush=True,
    )
    return r


def cmd_run(a):
    _run(
        a.name,
        a.kind,
        a.seed,
        a.agent,
        params=json.loads(a.params),
        window_ms=a.window_ms,
        workers=a.workers,
        slot=a.slot,
    )


def cmd_demo(a):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    rs = [
        _run(f"demo_{stamp}_entity_destruction_{ag}", "entity_destruction", SEED_WORKER, ag, slot=a.slot)
        for ag in ("noop", "oracle")
    ]
    print("\nentity_destruction (victim: one of two workers)")
    for r in rs:
        if not r["error"]:
            print(one_line(r["config"]["agent"], r["scores"]))


def cmd_jitter(a):
    stamp = a.stamp or time.strftime("%Y%m%dT%H%M%S")
    seeds = {"entity_destruction": SEED_WORKER, "belt_cut": 1}
    for kind in a.kinds:
        for i in range(a.n):
            _run(
                f"jitter_{stamp}_{kind}_{a.agent}_{i:02d}",
                kind,
                seeds.get(kind, 1),
                a.agent,
                slot=a.slot,
                work_mode=a.work_mode,
                image=a.image,
            )
    summarize(sorted((ROOT / "runs").glob(f"jitter_{stamp}_*")))


def cmd_floor(a):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    seeds = {"entity_destruction": SEED_GATEWAY, "belt_cut": 1, "resource_exhaustion": 1, "adaptive_strike": 1}
    rs = []
    for kind in a.kinds:
        for ag in a.agents:
            rs.append(_run(f"floor_{stamp}_{kind}_{ag}", kind, seeds[kind], ag, slot=a.slot))
    print("\nkind                  agent        TR      raw     recovered  TTR(ms)")
    for r in rs:
        if r["error"] or not r["scores"]["fires"]:
            print(f"{r['config']['kind']:<21} {r['config']['agent']:<12} no fire / error")
            continue
        f = r["scores"]["fires"][0]
        print(
            f"{r['config']['kind']:<21} {r['config']['agent']:<12} {f['tr']:.3f}   {f['tr_raw']:.3f}   {f['recovered']!s:<9}  {f['ttr_ms']}"
        )


def summarize(run_dirs, out=None):
    lines = []

    def emit(*a):
        lines.append(" ".join(str(x) for x in a))

    rows = []
    for d in run_dirs:
        ep = json.loads((d / "episode.json").read_text())
        if ep["error"] or not ep.get("scores") or not ep["scores"]["fires"]:
            rows.append((d.name, ep["config"]["kind"], ep["config"]["agent"], None))
            continue
        rows.append((d.name, ep["config"]["kind"], ep["config"]["agent"], ep["scores"]["fires"][0]))
    groups = {}
    for name, kind, agent, f in rows:
        groups.setdefault((kind, agent), []).append((name, f))
    emit(
        "\n| kind | agent | n | baseline mean (jobs/min) | baseline SD | TR mean | TR SD | TR CV | fire tick mean (s) | fire tick SD (s) | errors |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    for (kind, agent), items in sorted(groups.items()):
        ok = [f for _, f in items if f]
        err = len(items) - len(ok)
        if not ok:
            emit(f"| {kind} | {agent} | {len(items)} | - | - | - | - | - | - | - | {err} |")
            continue
        b = [f["baseline_per_min"] for f in ok]
        tr = [f["tr"] for f in ok]
        ft = [f["fire_tick"] / 1000 for f in ok]

        def sd(xs):
            return statistics.stdev(xs) if len(xs) > 1 else 0.0

        cv = sd(tr) / statistics.mean(tr) if statistics.mean(tr) else float("nan")
        emit(
            f"| {kind} | {agent} | {len(ok)} | {statistics.mean(b):.1f} | {sd(b):.2f} | {statistics.mean(tr):.4f} | {sd(tr):.4f} | {cv:.4f} | {statistics.mean(ft):.1f} | {sd(ft):.2f} | {err} |"
        )
    emit("\nper-run:")
    for name, kind, agent, f in rows:
        if f:
            emit(
                f"  {name}: base={f['baseline_per_min']:.1f} TR={f['tr']:.4f} raw={f['tr_raw']:.4f} fire={f['fire_tick']} recovered={f['recovered']} ttr={f['ttr_ms']}"
            )
        else:
            emit(f"  {name}: ERROR/no fire")
    text = "\n".join(lines)
    print(text)
    if out:
        Path(out).write_text(text + "\n")
    return text


def cmd_summarize(a):
    summarize(sorted(Path(p) for p in a.dirs), out=a.out)


def cmd_score(a):
    for d in a.dirs:
        d = Path(d)
        ep = json.loads((d / "episode.json").read_text())
        print(one_line(d.name, score_run(d, ep["config"]["window_ms"])))


def main(argv=None):
    p = argparse.ArgumentParser(prog="wrench_compose")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--kind", choices=KINDS, required=True)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--agent", choices=["noop", "restart_all", "oracle"], default="noop")
    r.add_argument("--params", default="{}")
    r.add_argument("--window-ms", type=int, default=120000)
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--slot", type=int, default=0)
    r.set_defaults(fn=cmd_run)
    d = sub.add_parser("demo")
    d.add_argument("--slot", type=int, default=0)
    d.set_defaults(fn=cmd_demo)
    j = sub.add_parser("jitter")
    j.add_argument("--kinds", nargs="+", default=["entity_destruction", "belt_cut"])
    j.add_argument("--n", type=int, default=10)
    j.add_argument("--agent", default="noop")
    j.add_argument("--stamp", default=None)
    j.add_argument("--slot", type=int, default=0)
    j.add_argument("--work-mode", default="cputime", choices=["iters", "cputime"])
    j.add_argument("--image", default="wrench-svc:local")
    j.set_defaults(fn=cmd_jitter)
    f = sub.add_parser("floor")
    f.add_argument("--kinds", nargs="+", default=KINDS)
    f.add_argument("--agents", nargs="+", default=["noop", "restart_all", "oracle"])
    f.add_argument("--slot", type=int, default=0)
    f.set_defaults(fn=cmd_floor)
    s = sub.add_parser("summarize")
    s.add_argument("dirs", nargs="+")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_summarize)
    sc = sub.add_parser("score")
    sc.add_argument("dirs", nargs="+")
    sc.set_defaults(fn=cmd_score)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
