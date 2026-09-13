"""A scripted operator as an Inspect model: ``compose-scripted/operator``.

It reads nothing but what a real model would (the system prompt, the
observations, its own previous replies) and answers with one ``wrenchctl``
command per turn, so an episode driven by it proves that the model path
(observation -> reply -> sandbox shell -> probe) can recover from a fault,
not just the fixture agents that read the fired manifest.

The policy is a fixed decision list over the ``wrenchctl ps`` /
``wrenchctl metrics`` blocks of the latest observation:

1. a service with fewer running containers than its replica count is
   rebuilt (``scale``), then reported;
2. otherwise, when throughput has collapsed against steady admissions, the
   worker logs are read once; ``pg insert failed`` lines mean the database
   is the fault (revoke the foreign role's connections), anything else with
   a backed-up queue means the worker->redis path (re-route around the
   ``netproxy`` hop); each repair is followed by a report;
3. otherwise ``wrenchctl metrics``.

``compose-scripted/noop`` answers ``wrenchctl metrics`` every turn.
"""

import re
from collections.abc import Sequence
from typing import Any

from inspect_ai.model import ChatMessage, GenerateConfig, ModelAPI, ModelOutput, modelapi

SERVICES = ("gateway", "redis", "worker", "postgres")
PG_REPAIR = (
    "wrenchctl exec postgres -- psql -U postgres -d factory -c "
    '"ALTER ROLE analytics CONNECTION LIMIT 0; '
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='analytics';\""
)
REDIS_REPAIR = "wrenchctl config edit worker REDIS_URL=redis://redis:6379/0"
OBSERVE = "wrenchctl metrics"


def _block(text: str, label: str) -> str:
    m = re.search(rf"`wrenchctl {label}`:\n```\n(.*?)\n```", text, re.DOTALL)
    return m.group(1) if m else ""


def running_by_service(ps_text: str) -> dict:
    counts = {s: 0 for s in SERVICES}
    for line in ps_text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3 and cols[0] in counts and cols[2] == "running":
            counts[cols[0]] += 1
    return counts


def _metric(metrics_text: str, key: str) -> float | None:
    m = re.search(rf"^{re.escape(key)}\s+(-|[0-9.]+)", metrics_text, re.MULTILINE)
    if not m or m.group(1) == "-":
        return None
    return float(m.group(1))


def _trailing(metrics_text: str, key: str) -> float | None:
    m = re.search(rf"^{re.escape(key)}.*trailing 30 s: (-|[0-9.]+)\)", metrics_text, re.MULTILINE)
    if not m or m.group(1) == "-":
        return None
    return float(m.group(1))


def expected_replicas(system_text: str) -> dict:
    m = re.search(r"worker\s+(\d+) replicas", system_text)
    return {"gateway": 1, "redis": 1, "worker": int(m.group(1)) if m else 2, "postgres": 1}


class ScriptedOperator:
    """Pure policy: ``next_command(messages)`` over (role, text) pairs."""

    def next_command(self, messages: Sequence[tuple[str, str]]) -> str:
        system = next((t for r, t in messages if r == "system"), "")
        users = [t for r, t in messages if r == "user"]
        past = [t.strip() for r, t in messages if r == "assistant"]
        past_cmds = [_command_of(t) for t in past]
        if not users:
            return OBSERVE
        obs = users[-1]
        expected = expected_replicas(system)
        running = running_by_service(_block(obs, "ps"))
        reported = {c.split()[2] for c in past_cmds if c.startswith("wrenchctl report_fault ") and len(c.split()) > 2}

        short = [s for s in SERVICES if running[s] < expected[s]]
        if short:
            svc = short[0]
            want = expected[svc]
            scale = f"wrenchctl scale {svc} {want}"
            if past_cmds and past_cmds[-1] == scale:
                return "wrenchctl ps"  # the rebuild is in progress; look again
            return scale
        for cmd in past_cmds:
            if cmd.startswith("wrenchctl scale "):
                svc = cmd.split()[2]
                if svc not in reported:
                    return f"wrenchctl report_fault {svc} '{svc} container was killed; rebuilt it with scale'"

        metrics = _block(obs, "metrics")
        done_rate = _trailing(metrics, "jobs_done")
        admitted_rate = _trailing(metrics, "gateway.admitted")
        uptime = _metric(metrics, "uptime_s") or 0.0
        collapsed = (
            uptime >= 30
            and admitted_rate is not None
            and admitted_rate >= 100
            and done_rate is not None
            and done_rate < 0.5 * admitted_rate
        )
        if collapsed:
            if REDIS_REPAIR in past_cmds and "worker" not in reported:
                return "wrenchctl report_fault worker 'workers cannot reach the queue through netproxy'"
            if PG_REPAIR in past_cmds and "postgres" not in reported:
                return "wrenchctl report_fault postgres 'connection slots exhausted by role analytics'"
            if REDIS_REPAIR in past_cmds or PG_REPAIR in past_cmds:
                return OBSERVE  # repaired and reported; let the rate come back
            if not past_cmds or not past_cmds[-1].startswith("wrenchctl logs worker"):
                return "wrenchctl logs worker --tail 20"
            logs = obs.split("\n---\n", 1)[0]  # the previous command's result
            if "pg insert failed" in logs:
                return PG_REPAIR
            return REDIS_REPAIR
        return OBSERVE


def _command_of(reply: str) -> str:
    m = re.search(r"```[a-z]*\n(.*?)\n```", reply, re.DOTALL)
    return (m.group(1) if m else reply).strip().splitlines()[0].strip() if reply.strip() else ""


class ScriptedModelAPI(ModelAPI):
    """``compose-scripted/operator`` (the policy above) and
    ``compose-scripted/noop`` (always ``wrenchctl metrics``)."""

    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig = GenerateConfig(),
        **model_args: Any,
    ) -> None:
        super().__init__(model_name, base_url, api_key, [], config)
        self.policy = ScriptedOperator() if model_name != "noop" else None

    async def generate(self, input: list[ChatMessage], tools, tool_choice, config: GenerateConfig) -> ModelOutput:
        messages = [(m.role, m.text) for m in input]
        command = self.policy.next_command(messages) if self.policy else OBSERVE
        return ModelOutput.from_content(model=f"compose-scripted/{self.model_name}", content=f"```sh\n{command}\n```")


@modelapi(name="compose-scripted")
def compose_scripted():
    return ScriptedModelAPI
