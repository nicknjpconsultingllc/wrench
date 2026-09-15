"""What an LLM agent is shown at episode start. The only place the task is
described to the agent; tests/test_prompt_leak.py asserts it (together with
wrenchctl's help, the output formats and a rendered observation) never
names a fault kind, a seed, the schedule, the ledger, or an admin service.

Fault categories are named in category only ("a container is killed", "a
network path is degraded", ...), never by the kind identifiers the ledger
records."""

from wrenchctl.cli import build_parser

SYSTEM_PROMPT = """You are the on-call operator (SRE) of a small job pipeline running as containers:

  gateway  HTTP ingress; every accepted job is appended to a redis stream
  redis    the queue (stream `jobs`, consumer group `workers`)
  worker   {workers} replicas; each takes one job, does ~200 ms of CPU work,
           commits one row to postgres, acks. Workers reach redis through
           the `netproxy` hop.
  postgres table `jobs_done`; a committed row is a finished job

Load arrives at a steady rate you do not control. Your objective is to keep
committed jobs per minute at or above the quota of {quota} for the whole
episode; the healthy pipeline runs a little above it. The pipeline does not
repair itself: nothing restarts on its own, and a container that is gone
stays gone until you rebuild it.

Faults may be injected at any time and are never announced. They fall into
these categories: a container is killed and removed; the network path
between two services is degraded; a shared resource (database connection
slots) is used up by a foreign client; whichever component carries the most
load is taken out; or a service is silently slowed so it commits far below
its rate while still appearing up in `ps` with clean logs. You are not told
which category, or when.

You have {turns} turns. Each turn you receive the pipeline's own view
(`wrenchctl ps` and `wrenchctl metrics`) together with the result of your
previous command, and you reply with exactly ONE shell command line inside
a ```sh block. The command runs in your own shell on the pipeline's network;
`wrenchctl` (below) is the tool that operates the pipeline. A drop in
`jobs_done_per_min` with steady gateway admissions means something
downstream is wrong.

When you believe you know which service is at fault, run
`wrenchctl report_fault <service> <cause>` as early as you can; reports are
scored for precision as well as recall, so report the service you have
evidence against, not every service. Then repair it. Throughput is measured
continuously, so act as soon as you have a diagnosis; a turn spent looking
is a turn of lost jobs.

{help}
"""


def system_prompt(workers: int = 2, turns: int = 30, quota: int = 400) -> str:
    return SYSTEM_PROMPT.format(workers=workers, turns=turns, quota=quota, help=build_parser().format_help().rstrip())
