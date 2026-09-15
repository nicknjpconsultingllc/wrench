"""The compose substrate's scoring configuration for ``wrench_core``.

One tick is one millisecond since the probe started, so the Factorio
defaults (60 ticks/s) are rebased: a 1-minute baseline window and a
30-second trailing window, in ms. The ``entity_destruction`` and
``adaptive_strike`` manifests carry ``same_type_total`` with Factorio's
meaning (chaos counts the victim service's running replicas at fire time,
before the kill: ``len(running(service))`` in ``chaos/main.py``), so the
redundancy floor applies to both kill kinds. ``belt_cut``,
``resource_exhaustion`` and ``silent_throttle`` also write the field, but
nothing is destroyed there, so it is informational and they stay out of
``redundancy_kinds`` (floor-adjusted TR stays ``None``).
Reports and fires carry x/y positions (``wrench_compose.positions``), so
the default position matcher is kept.
"""

from wrench_core.scoring import ScoringConfig

MS_PER_MINUTE = 60000
# Kinds that destroy exactly one replica of a service whose replica count
# at fire time is a trustworthy passive-redundancy floor.
REDUNDANCY_KINDS = frozenset({"entity_destruction", "adaptive_strike"})

COMPOSE_CONFIG = ScoringConfig(
    ticks_per_minute=MS_PER_MINUTE,
    trailing_window_ticks=MS_PER_MINUTE // 2,
    baseline_window_ticks=MS_PER_MINUTE,
    redundancy_kinds=REDUNDANCY_KINDS,
)
