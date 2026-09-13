# Substrates

One directory per environment that produces WRENCH episodes. A substrate owns
fault injection, sampling and agent I/O; it hands `wrench_core` a list of
`{"tick": int, "counts": {item: cumulative_count}}` samples and a list of
`LedgerEntry.model_dump()` dicts, and gets the metrics back.

- Factorio: lives in the fork at `factorioBenchmark` (`fle/disruptions/`,
  `fle/eval/inspect/`), not here.
- `compose/`: a docker-compose job pipeline, wall-clock ticks in ms. Four
  fault kinds pass the floor test against no-op and restart-all fixtures;
  the oracle brackets at TR ≈ 1. See its README and `docs/jitter_study.md`.
