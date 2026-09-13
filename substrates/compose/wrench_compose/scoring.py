"""The compose substrate's scoring configuration for ``wrench_core``.

One tick is one millisecond since the probe started, so the Factorio
defaults (60 ticks/s) are rebased: a 1-minute baseline window and a
30-second trailing window, in ms. Fault manifests carry the same
``same_type_total`` field as Factorio's ``entity_destruction`` and it means
the same thing (replica count at fire time), so the redundancy floor applies
to that kind unchanged. Reports and fires carry x/y positions
(``wrench_compose.positions``), so the default position matcher is kept.
"""

from wrench_core.scoring import ScoringConfig

MS_PER_MINUTE = 60000

COMPOSE_CONFIG = ScoringConfig(
    ticks_per_minute=MS_PER_MINUTE,
    trailing_window_ticks=MS_PER_MINUTE // 2,
    baseline_window_ticks=MS_PER_MINUTE,
)
