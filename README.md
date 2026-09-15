# WRENCH

One measurement contract, two substrates. `wrench_core` holds the
substrate-agnostic scorers for disruption-recovery episodes: frozen-baseline
throughput retained (plain and redundancy-floor-adjusted), recovery-at-budget,
right-censored time-to-recovery with a Kaplan-Meier pooler, bipartite
detection precision/recall with an anti-spam gate, and the potential-based
reward-shaping term. Every scorer takes the same inputs: a list of
`{"tick", "counts"}` production samples and a list of ledger events. The
Factorio substrate is the fork at `../factorioBenchmark` (it will import
`wrench_core` instead of its own `fle/disruptions/scoring.py`); the
docker-compose substrate lives under `substrates/compose/`: a gateway →
redis → workers → postgres job pipeline with an HMAC-verified throughput
probe, four fault kinds, and no-op / restart-all / oracle fixture agents.
Its jitter study and floor tests are in `substrates/compose/docs/`.
`docs/porting.md` lists what changed relative to the fork.

The project [writeup](docs/writeup.md) covers the measurement contract, the
adversarial hardening record, and the first model results across both
substrates.

```
uv venv --python 3.12 && uv sync --group dev
uv run pytest tests -q
```

```
cd substrates/compose && make venv && make test   # 21 unit tests, no Docker
make demo                                          # no-op vs oracle, needs Docker
```
