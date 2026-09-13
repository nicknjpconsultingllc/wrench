"""Deterministic victim selection (mirror of server.lua's seeded LCG)."""


def seeded_index(seed: int, n: int) -> int | None:
    """One LCG step on the seed; 0-based index into a list of n candidates."""
    if n == 0:
        return None
    x = (seed * 1103515245 + 12345) % 2147483648
    return x % n


def pick_seeded(seed: int, candidates: list[str]) -> str | None:
    ordered = sorted(candidates)
    idx = seeded_index(seed, len(ordered))
    return None if idx is None else ordered[idx]


def pick_adaptive(inflight: dict[str, float], seed: int) -> str | None:
    """Holder of the most in-flight work; ties broken by the seed."""
    if not inflight:
        return None
    top = max(inflight.values())
    if top <= 0:
        return None
    tied = [k for k, v in inflight.items() if v == top]
    return pick_seeded(seed, tied)


def load_bearing(flow: dict[str, float], replicas: dict[str, int]) -> dict[str, float]:
    """Score = share of window throughput that traversed the holder, divided
    by the holder's replica count: the capacity its loss removes. Keys are
    holders ("gateway", "redis", "worker:<host>"); `replicas` is keyed by
    service (the part before ':'). Holders with unknown service or zero flow
    score 0."""
    total = max(flow.values(), default=0.0)
    if total <= 0:
        return {k: 0.0 for k in flow}
    out = {}
    for holder, f in flow.items():
        service = holder.split(":", 1)[0]
        n = replicas.get(service, 0)
        out[holder] = (f / total) / n if n > 0 else 0.0
    return out
