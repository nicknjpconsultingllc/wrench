# WRENCH: measuring whether an agent can keep a system alive

Most agent benchmarks ask whether a model can build something. WRENCH asks
whether it can keep something running after the ground shifts under it. An
agent brings a system up to a throughput target. Once the system demonstrably
works, a fault strikes it and nothing is announced. The agent has to notice
that throughput dropped, diagnose what broke, and repair it while the loss
compounds. That is the on-call skill, and it is the part of real operations
work that a "build a plan" benchmark never touches.

The contribution here is the measurement. WRENCH is a scoring contract for
disruption recovery plus the infrastructure that makes the numbers hard to
fake: a ground-truth signal the agent cannot read, a baseline fixed before the
fault so it cannot be moved, fixture agents that bracket the metric before any
model runs, and an acceptance test every fault has to pass. The same contract
runs against two unrelated systems, which is how we know it measures recovery
and travels beyond one game.

## The measurement contract

An independent probe samples throughput and owns the ledger. The agent never
sees either.

- **Frozen baseline.** The probe records the pre-fault throughput rate and
  freezes it. Throughput Retained is the pooled ratio of post-fault
  production to that frozen rate over a fixed window. Zero means the agent did
  no better than walking away; one means full recovery.
- **Precondition-gated arming.** A fault arms only after throughput holds at
  or above the target for two consecutive windows. The agent cannot be
  disrupted before it has something to lose, so the denominator stays bounded
  away from zero.
- **Detection as an overt act.** The agent declares faults through a
  `report_fault` tool. Detection is scored as precision, recall and latency
  against the ledger. A report is credited to a fault only if it names the
  right component and lands after the fault fired. Reports are capped one per
  fault and one per report, and recall is gated to zero when precision falls
  below a floor, so spamming reports lowers the score instead of raising it.
- **Floor acceptance.** A fault ships only if a no-op agent loses at least
  80% of throughput over the window. A fault a restart heals by itself does
  not measure recovery and does not ship.
- **Three views of retention.** Winsorized TR is the headline. Raw TR shows
  overbuild past baseline. Floor-adjusted TR removes the throughput a passive
  survivor would have produced, using a redundancy count fixed at fault time,
  so a factory that survives on spare capacity earns no recovery credit.
- **Time to recovery** is a Kaplan-Meier curve over the per-fault recovery
  times, right-censored at the window end, never a mean that quietly drops the
  runs that never recovered.

## One contract, two substrates

`wrench_core` holds the scorers and knows nothing about either system. It
takes a list of `{tick, counts}` samples and a list of ledger events and
returns the numbers. A `ScoringConfig` carries the tick unit, the windows, the
report-to-fault matcher and the redundancy-eligible fault kinds, so a
wall-clock system and a game-tick system run the identical math.

- **Factorio.** A hard fork of the Factorio Learning Environment. The agent
  writes Python against a live game server to build an automated factory to a
  production quota. A Lua scheduler inside the game fires seeded faults in game
  time: a machine destroyed, a belt line cut, an ore patch exhausted, or a
  strike on whatever entity is carrying the most load.
- **Services.** A docker-compose pipeline: an HTTP gateway feeds a Redis
  stream, worker replicas commit rows to Postgres, a load generator drives a
  steady request rate. The agent operates it through a locked-down shell in a
  sandbox container. Faults map across cleanly: kill a worker, degrade the
  worker-to-Redis network path, exhaust the Postgres connection pool from a
  foreign client, or take out whichever component carries the most load.

The two substrates share `wrench_core` and one test suite. Three real Factorio
episodes are checked into the core's tests, and their scores through
`wrench_core` match the fork's original scorers to the float. That equality is
the claim that the extraction changed nothing and that the contract is
substrate-agnostic.

## Validity before any model runs

Every metric is bracketed by scripted agents in CI:

- a **no-op** agent that ignores the fault scores TR near the floor,
- an **oracle** that repairs it immediately scores TR near one,
- a **restart-all** agent that blindly bounces every service also has to fail
  the floor, which is the exploit that beats a large fraction of one published
  Kubernetes incident benchmark. On the services substrate it stays broken:
  the destroyed worker is gone, the network toxic and the pool hog survive a
  restart.

The services substrate went through a jitter study before it was trusted to
discriminate. Replaying the same seeded fault against a no-op agent, the
retained-throughput coefficient of variation has to sit at or below 0.05 or
the benchmark has no resolving power. It passed, but only after the per-job
work was pinned to CPU time rather than a fixed iteration count. On Apple
Silicon the iteration count drifted with core placement and pushed the
coefficient to the edge of the bar. Fixing the work term dropped it to 0.003.

## Adversarial hardening

The scoring and the harness were attacked before any money was spent, in
several rounds, each with an independent reviewer per lens and a reproduction
required for every finding. The full record is `docs/hardening.md` in the
Factorio fork repository; it is summarized here.

On the Factorio substrate, sixteen findings across seven rounds. Three were
reward-hacking paths: a free `report_fault` that scored spam like real
detection, a redundancy floor an agent could lower by sabotaging its own
survivor, and a detection guard that could push precision above one. Six were
silent failures that would have produced a clean-looking results table full of
wrong numbers, including a server-pool timeout calibrated against the wrong
reference that cost fifteen of eighteen paid episodes on the first grid before
it was diagnosed from the real episode durations.

One finding was first written up as remote code execution and then downgraded
after a red-team pass. A string-encoding bug let a crafted tool argument break
out of its Lua literal, but the agent's program already runs in-process with
the game instance in scope, so it can reach the server directly with no trick.
The encoding bug is real as a correctness issue and was fixed and sent upstream
as one. The finding that survived is larger: ground truth is tamper-resistant
against in-game actions, not against a Python program that reaches for the
server. A sandboxed executor is required before any reinforcement-learning use,
and the services substrate was built with exactly that boundary.

On the services substrate, a four-lens review before the first paid grid found
eleven issues. The one that would have wasted a whole model's budget was an
output-token cap that would have blanked every reasoning model into a
zero-action run that reads as incompetence. The one that mattered most for the
signal was that the training reward used plain TR, so an agent could scale to
four workers on turn one and idle to a near-perfect score. The reward is now
floor-adjusted, so pre-fault overbuild earns nothing. The first launch then
died in sixty seconds at zero cost: docker resolved a relative secrets path
against the wrong directory. The fix was one line and the diagnosis came from
teaching the command wrapper to carry its stderr.

## First results

Three models, four fault kinds, five seeds each on the services substrate,
sixty episodes, all completed, $17.66. A two-seed pilot ran first at $7.44 and
set the design and the fixes below; the five-seed grid is the result. The table
is pooled Throughput Retained with a bootstrap 95% interval over the five
seeds in each cell.

| kind | Claude Sonnet 5 | GPT-5.1 | Gemini 2.5 Pro |
|---|---|---|---|
| entity_destruction | 0.98 [0.95, 1.00] | 0.98 [0.94, 1.00] | 0.97 [0.91, 1.00] |
| belt_cut | 0.60 [0.33, 0.87] | 0.94 [0.87, 1.00] | 0.39 [0.17, 0.61] |
| resource_exhaustion | 0.74 [0.46, 0.96] | 0.08 [0.01, 0.15] | 0.00 [0.00, 0.00] |
| adaptive_strike | 0.91 [0.90, 0.93] | 0.86 [0.85, 0.88] | 0.81 [0.75, 0.86] |

**Connection-pool exhaustion is the headline, and the separation is clean.**
Sonnet recovers it on four of five seeds; GPT-5.1 and Gemini recover it on none
of ten attempts between them, and their intervals sit far below Sonnet's with
no overlap. The reason is legible in the logs: recovery needs both freeing the
slots the foreign client holds and getting the workers to reconnect, and only
Sonnet did both. This is the cell a benchmark exists to produce, adjacent
frontier models separated with confidence for a readable reason.

**Two more kinds separate the models, one saturates.** belt_cut splits GPT-5.1
at 0.94 from Gemini at 0.39 with non-overlapping intervals, a gap the two-seed
pilot hid entirely when all three sat above 0.74. adaptive_strike is a tight
monotonic ordering, Sonnet above Gemini with no overlap. entity_destruction is
saturated: every model recovers a killed container, so it measures nothing here.

**Detection recall saturates; precision is the signal.** Every model reported
every fault, recall 1.0 in every cell, because the pipeline view makes a killed
or degraded component obvious. Precision is where the models part: on the three
harder kinds it fell below 1.0 as models reported the wrong service, GPT-5.1
most. The `silent_throttle` kind, a Postgres trigger that slows every commit
while every container stays healthy, exists to make recall earn its keep. A
status-only agent that reads only the container list scores recall 0 on it and
recall 1 on a container kill.

**belt_cut measures diagnosis.** A flagged anomaly in that column was chased
down with a live experiment and the grid ledger. Recovery there requires one
specific repair, rerouting the workers off the degraded network path with a
config change. A plain restart or a rescale leaves throughput at zero because
the new connections inherit the same degraded path. Every model found the
reroute. The network toxic survives it, so the kind measures whether the agent
finds the one env change and does not test resilience to a fault it cannot
dodge. That limit is stated here rather than hidden.

## Honest edges

- Five seeds per cell is a pilot scale. The intervals above are a bootstrap
  over five seeds in one run on one host; a published result would want more
  seeds, more models, and the paired common-random-number design the harness
  already supports.
- The numbers were self-audited before this repository went public. External
  scrutiny is the point of publishing it, and the ledgers and samples ship
  alongside so anyone can re-score the table.
- There is no reinforcement-learning transfer evidence. The reward shaping is
  built and statically validated, the telescoping identity holds on every real
  episode, but no model has been trained on WRENCH. Claiming otherwise would be
  dishonest, and the resources to run real training are not in hand.
- Ground truth is not enforced against an adversarial program on the Factorio
  substrate. The services substrate closes that with a sandbox, and any
  training use of either needs that boundary first.

## What this is

This is grading infrastructure, a measurement-validity argument, and a failure
taxonomy, with two substrates as evidence that the contract travels. Factorio
is one section. The disruption-recovery contract, the frozen baseline, the
overt-detection scoring, the floor test, and the fixture brackets port to any
system with a throughput signal and a way to inject a fault, which is what the
services substrate demonstrates. The reproductions, ledgers and trajectories
ship with the numbers so anyone can re-score them.
