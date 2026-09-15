# WRENCH compose comparison table

- models: openrouter/anthropic/claude-sonnet-5, openrouter/openai/gpt-5.1, openrouter/google/gemini-2.5-pro
- kinds: entity_destruction, belt_cut, resource_exhaustion, adaptive_strike
- seeds: 1, 3
- turns per episode: 30
- generated: 2026-09-12 23:55:13

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
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 2/2 | 2 | 0.940 | 0.940 | 0.940 | 1.00 | 35498 | 1.00 | 1.00 | 1.00 | 6896 |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 2/2 | 2 | 0.739 | 0.739 | - | 1.00 | 65500 | 1.00 | 1.00 | 1.00 | 40685 |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 2/2 | 2 | 0.965 | 0.965 | 0.953 | 1.00 | 30501 | 1.00 | 1.00 | 1.00 | 7990 |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 2/2 | 2 | 0.635 | 0.635 | - | 1.00 | 98500 | 1.00 | 1.00 | 1.00 | 27712 |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 2/2 | 2 | 0.842 | 0.842 | 0.842 | 1.00 | 46000 | 1.00 | 1.00 | 1.00 | 11688 |
| openrouter/google/gemini-2.5-pro | belt_cut | 2/2 | 2 | 0.795 | 0.795 | - | 1.00 | 52992 | 1.00 | 1.00 | 1.00 | 45390 |
| openrouter/google/gemini-2.5-pro | entity_destruction | 2/2 | 2 | 0.911 | 0.911 | 0.881 | 1.00 | 38501 | 1.00 | 1.00 | 1.00 | 16018 |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 2/2 | 2 | 0.072 | 0.072 | - | 0.00 | - | 1.00 | 1.00 | 1.00 | 46192 |
| openrouter/openai/gpt-5.1 | adaptive_strike | 2/2 | 2 | 0.849 | 0.849 | 0.849 | 1.00 | 46493 | 1.00 | 0.50 | 0.50 | 11632 |
| openrouter/openai/gpt-5.1 | belt_cut | 2/2 | 2 | 1.000 | 1.000 | - | 1.00 | 45997 | 1.00 | 1.00 | 1.00 | 32011 |
| openrouter/openai/gpt-5.1 | entity_destruction | 2/2 | 2 | 0.946 | 0.946 | 0.929 | 1.00 | 31500 | 1.00 | 1.00 | 1.00 | 11575 |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 2/2 | 2 | 0.130 | 0.130 | - | 0.50 | 119501 | 1.00 | 1.00 | 1.00 | 21200 |

## Per-episode results

| Model | Kind | Seed | Status | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall | Error |
|---|---|---|---|---|---|---|---|---|---|---|
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 1 | success | 1 | 0.931 | 0.931 | 0.931 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | entity_destruction | 3 | success | 1 | 0.998 | 0.998 | 0.996 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 1 | success | 1 | 0.640 | 0.640 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | belt_cut | 3 | success | 1 | 0.838 | 0.838 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 1 | success | 1 | 0.581 | 0.581 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | resource_exhaustion | 3 | success | 1 | 0.688 | 0.688 | - | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 1 | success | 1 | 0.938 | 0.938 | 0.938 | 1.00 | 1.00 |  |
| openrouter/anthropic/claude-sonnet-5 | adaptive_strike | 3 | success | 1 | 0.943 | 0.943 | 0.943 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 1 | success | 1 | 0.895 | 0.895 | 0.895 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | entity_destruction | 3 | success | 1 | 0.998 | 0.998 | 0.996 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 1 | success | 1 | 1.000 | 1.000 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | belt_cut | 3 | success | 1 | 1.000 | 1.000 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 1 | success | 1 | 0.257 | 0.257 | - | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | resource_exhaustion | 3 | success | 1 | 0.003 | 0.003 | - | 0.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 1 | success | 1 | 0.849 | 0.849 | 0.849 | 1.00 | 1.00 |  |
| openrouter/openai/gpt-5.1 | adaptive_strike | 3 | success | 1 | 0.848 | 0.848 | 0.848 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 1 | success | 1 | 0.821 | 0.821 | 0.821 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | entity_destruction | 3 | success | 1 | 1.000 | 1.000 | 1.000 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 1 | success | 1 | 0.635 | 0.635 | - | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | belt_cut | 3 | success | 1 | 0.955 | 0.955 | - | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 1 | success | 1 | 0.142 | 0.142 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | resource_exhaustion | 3 | success | 1 | 0.002 | 0.002 | - | 0.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 1 | success | 1 | 0.835 | 0.835 | 0.835 | 1.00 | 1.00 |  |
| openrouter/google/gemini-2.5-pro | adaptive_strike | 3 | success | 1 | 0.850 | 0.850 | 0.850 | 1.00 | 1.00 |  |
