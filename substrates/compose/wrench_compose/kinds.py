"""The compose substrate's fault kinds and the seeds each driver defaults to.

Seeds are chosen against the default 2-worker topology (candidates sorted:
gateway-1, worker-1, worker-2): seed 3 picks worker-1 (redundant, no-op TR
~0.6, the bracketing case), seed 1 picks gateway-1 (single point of
failure, the floor case). The fixture CLI, the Inspect task and the
verifiers environment all read this module so the kind list cannot drift.
"""

KINDS = ["entity_destruction", "belt_cut", "resource_exhaustion", "adaptive_strike", "silent_throttle"]
SEED_WORKER = 3
SEED_GATEWAY = 1
DEFAULT_SEED = SEED_GATEWAY


def parse_kinds(kinds) -> list[str]:
    """A list or comma-separated string of kinds; None means all four."""
    if kinds is None:
        return list(KINDS)
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValueError(f"unknown compose fault kind(s) {unknown}; choose from {KINDS}")
    return list(kinds)


# Kinds that leave every container up and healthy in `ps` and print no error
# line in any `logs`: the only evidence is the throughput signal. Recorded here
# so a status-only detector and its tests agree on which kinds it is blind to.
STEALTH_KINDS = frozenset({"silent_throttle"})


def parse_seeds(seeds) -> list[int]:
    """A list, an int, or a comma-separated string of seeds ("1,3")."""
    if seeds is None:
        return [DEFAULT_SEED]
    if isinstance(seeds, int):
        return [seeds]
    if isinstance(seeds, str):
        seeds = [s.strip() for s in seeds.split(",") if s.strip()]
    return [int(s) for s in seeds]
