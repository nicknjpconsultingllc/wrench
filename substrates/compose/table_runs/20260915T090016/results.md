# WRENCH compose comparison table

- models: openrouter/anthropic/claude-sonnet-5, openrouter/openai/gpt-5.1, openrouter/google/gemini-2.5-pro
- kinds: entity_destruction, belt_cut, resource_exhaustion, adaptive_strike
- seeds: 1, 2, 3, 4, 5
- turns per episode: 30
- generated: 2026-09-15 10:39:36

Aggregates use the pooled-ratio rule: sum of raw numerators over sum
of raw denominators across seeds/fires (never mean-of-ratios).
`-` = not scoreable (no fires with a valid frozen baseline).
TR is winsorized to [-0.5, 1.5] (wrench_core.scoring) and is the
headline metric; TR (raw) is the same pooled ratio before the clamp.
TR (floor-adj) subtracts the passive-redundancy floor (entity_destruction and
adaptive_strike fires carrying same_type_total) from numerator and denominator.
Det. recall is pooled matched fires / fires, gated to 0 when the pooled
loose precision is below 0.5 (the per-episode gate, applied to the pool);
`-` when the group had no fires.
TTR median is the Kaplan-Meier median time to sustained recovery in ms
over the group's fires (censored fires stay at risk until the window
end; `-` when fewer than half recovered).

## Per-(model, kind) aggregates

| Model | Kind | Episodes | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | TTR median (ms) | Det. recall | Det. precision (strict) | Det. precision (loose) | Det. latency (ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 5/5 | 5 | 0.911 | 0.911 | 0.911 | 1.00 | 39000 | 1.00 | 1.00 | 1.00 | 8905 |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 5/5 | 5 | 0.603 | 0.603 | - | 0.80 | 94000 | 1.00 | 0.83 | 0.83 | 48947 |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 5/5 | 5 | 0.983 | 0.983 | 0.971 | 1.00 | 30502 | 1.00 | 1.00 | 1.00 | 8309 |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 5/5 | 5 | 0.743 | 0.743 | - | 0.80 | 85500 | 1.00 | 0.71 | 0.71 | 29350 |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 5/5 | 5 | 0.813 | 0.813 | 0.813 | 1.00 | 49505 | 1.00 | 0.83 | 0.83 | 15707 |
| openrouter/google/gemini-2.5-pro | belt_cut | 5/5 | 5 | 0.386 | 0.386 | - | 0.60 | 114499 | 1.00 | 0.71 | 0.71 | 61696 |
| openrouter/google/gemini-2.5-pro | entity_destruction | 5/5 | 5 | 0.971 | 0.971 | 0.952 | 1.00 | 36500 | 1.00 | 1.00 | 1.00 | 15213 |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 5/5 | 5 | 0.003 | 0.003 | - | 0.00 | - | 1.00 | 0.83 | 0.83 | 40235 |
| openrouter/openai/gpt-5.1 | adaptive_strike | 5/5 | 5 | 0.865 | 0.865 | 0.865 | 1.00 | 44499 | 1.00 | 0.56 | 0.56 | 9487 |
| openrouter/openai/gpt-5.1 | belt_cut | 5/5 | 5 | 0.940 | 0.940 | - | 1.00 | 68501 | 1.00 | 0.62 | 0.62 | 39750 |
| openrouter/openai/gpt-5.1 | entity_destruction | 5/5 | 5 | 0.978 | 0.978 | 0.960 | 1.00 | 31499 | 1.00 | 0.83 | 0.83 | 9769 |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 5/5 | 5 | 0.080 | 0.080 | - | 0.00 | - | 1.00 | 0.71 | 0.71 | 16785 |

## Per-episode results

| Model | Kind | Seed | Status | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall | Error |
|---|---|---|---|---|---|---|---|---|---|---|
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 1 | success | 1 | 0.915 | 0.915 | 0.915 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 2 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 3 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 4 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 5 | success | 1 | 0.998 | 0.998 | 0.996 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 1 | success | 1 | 0.491 | 0.491 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 2 | success | 1 | 0.563 | 0.563 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 3 | success | 1 | 0.854 | 0.854 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 4 | success | 1 | 1.000 | 1.000 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 5 | success | 1 | 0.108 | 0.108 | - | 0.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 1 | success | 1 | 0.207 | 0.207 | - | 0.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 2 | success | 1 | 0.659 | 0.659 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 3 | success | 1 | 0.966 | 0.966 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 4 | success | 1 | 1.000 | 1.000 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 5 | success | 1 | 0.885 | 0.885 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 1 | success | 1 | 0.899 | 0.899 | 0.899 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 2 | success | 1 | 0.885 | 0.885 | 0.885 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 3 | success | 1 | 0.932 | 0.932 | 0.932 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 4 | success | 1 | 0.908 | 0.908 | 0.908 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 5 | success | 1 | 0.932 | 0.932 | 0.932 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 1 | success | 1 | 0.893 | 0.893 | 0.893 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 2 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 3 | success | 1 | 0.996 | 0.996 | 0.983 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 4 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 5 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 1 | success | 1 | 0.996 | 0.996 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 2 | success | 1 | 0.998 | 0.998 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 3 | success | 1 | 1.000 | 1.000 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 4 | success | 1 | 0.898 | 0.898 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 5 | success | 1 | 0.810 | 0.810 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 1 | success | 1 | 0.171 | 0.171 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 2 | success | 1 | 0.002 | 0.002 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 3 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 4 | success | 1 | 0.197 | 0.197 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 5 | success | 1 | 0.025 | 0.025 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 1 | success | 1 | 0.891 | 0.891 | 0.891 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 2 | success | 1 | 0.865 | 0.865 | 0.865 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 3 | success | 1 | 0.857 | 0.857 | 0.857 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 4 | success | 1 | 0.840 | 0.840 | 0.840 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 5 | success | 1 | 0.873 | 0.873 | 0.873 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 1 | success | 1 | 0.857 | 0.857 | 0.857 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 2 | success | 1 | 0.998 | 0.998 | 0.996 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 3 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 4 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 5 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 1 | success | 1 | 0.102 | 0.102 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 2 | success | 1 | 0.129 | 0.129 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 3 | success | 1 | 0.366 | 0.366 | - | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 4 | success | 1 | 0.754 | 0.754 | - | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 5 | success | 1 | 0.579 | 0.579 | - | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 1 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 2 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 3 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 4 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 5 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 1 | success | 1 | 0.824 | 0.824 | 0.824 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 2 | success | 1 | 0.698 | 0.698 | 0.698 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 3 | success | 1 | 0.870 | 0.870 | 0.870 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 4 | success | 1 | 0.817 | 0.817 | 0.817 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 5 | success | 1 | 0.856 | 0.856 | 0.856 | 1.00 | 1.00 |  |
