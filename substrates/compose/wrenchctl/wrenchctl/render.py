"""Human-readable rendering of API payloads. Pure functions so the prompt-leak
test can render them without a server."""


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header).rstrip()]
    for r in rows:
        lines.append(fmt.format(*r).rstrip())
    return "\n".join(lines)


def ps(payload: list[dict]) -> str:
    if not payload:
        return "(no containers)"
    rows = [[c["service"], c["name"], c["state"], (c.get("started_at") or "")[:19]] for c in payload]
    return _table(rows, ["SERVICE", "CONTAINER", "STATE", "STARTED"])


def logs(payload: dict[str, str]) -> str:
    out = []
    for name, text in payload.items():
        out.append(f"==> {name} <==")
        out.append(text.rstrip("\n"))
    return "\n".join(out) if out else "(no containers)"


def exec_(payload: dict) -> tuple[str, str, int]:
    return payload.get("stdout", ""), payload.get("stderr", ""), int(payload.get("exit_code") or 0)


def restart(payload: dict) -> str:
    return "restarted: " + ", ".join(payload.get("restarted", []))


def scale(payload: dict) -> str:
    parts = [f"running: {', '.join(payload.get('running', [])) or '-'}"]
    if payload.get("created"):
        parts.append(f"created: {', '.join(payload['created'])}")
    if payload.get("removed"):
        parts.append(f"removed: {', '.join(payload['removed'])}")
    return "\n".join(parts)


def config(payload: dict) -> str:
    env = payload.get("env", {})
    lines = [f"recreated: {', '.join(payload.get('recreated', [])) or '-'}", "env:"]
    lines += [f"  {k}={v}" for k, v in sorted(env.items())]
    return "\n".join(lines)


def _num(v):
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def metrics(payload: dict) -> str:
    gw = payload.get("gateway") or {}
    q = payload.get("queue") or {}
    lines = [
        f"uptime_s          {_num(payload.get('uptime_s'))}",
        f"gateway.admitted  {_num(gw.get('admitted'))}   (per min, trailing 30 s: {_num(gw.get('admitted_per_min'))})",
        f"gateway.rejected  {_num(gw.get('rejected'))}",
        f"gateway.inflight  {_num(gw.get('inflight'))}",
        f"queue.length      {_num(q.get('length'))}",
        f"queue.pending     {_num(q.get('pending'))}",
        f"queue.lag         {_num(q.get('lag'))}",
        f"jobs_done         {_num(payload.get('jobs_done'))}   (per min, trailing 30 s: {_num(payload.get('jobs_done_per_min'))})",
    ]
    workers = payload.get("workers") or {}
    for name in sorted(workers):
        w = workers[name]
        lines.append(f"  {name}: done={_num(w.get('done'))} {w.get('error') or ''}".rstrip())
    if payload.get("unreachable"):
        lines.append("unreachable: " + ", ".join(payload["unreachable"]))
    return "\n".join(lines)


def report_fault(payload: dict) -> str:
    return f"recorded (t={payload.get('tick')} ms)"
