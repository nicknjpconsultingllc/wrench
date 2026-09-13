"""The scripted operator decides from the observation alone."""

import asyncio

from inspect_ai.model import ChatMessageSystem, ChatMessageUser, get_model

from wrench_compose.episode import format_feedback, format_observation
from wrench_compose.policy import OBSERVE, PG_REPAIR, REDIS_REPAIR, ScriptedOperator, running_by_service
from wrench_compose.prompt import system_prompt

from fakes import METRICS_TEXT, PS_TEXT

SYSTEM = system_prompt(workers=2, turns=30)
PS_MISSING_WORKER = "\n".join(line for line in PS_TEXT.splitlines() if "worker-1" not in line)
PS_MISSING_GATEWAY = "\n".join(line for line in PS_TEXT.splitlines() if "gateway-1" not in line)
METRICS_COLLAPSED = METRICS_TEXT.replace("trailing 30 s: 478.0", "trailing 30 s: 40.0")


def obs(ps=PS_TEXT, metrics=METRICS_TEXT, feedback="ok", turn=0):
    return format_observation(feedback, turn, 30, ps, metrics)


def reply(cmd):
    return f"```sh\n{cmd}\n```"


def test_running_counts():
    assert running_by_service(PS_TEXT) == {"gateway": 1, "redis": 1, "worker": 2, "postgres": 1}
    assert running_by_service(PS_MISSING_WORKER)["worker"] == 1
    assert running_by_service(PS_TEXT.replace("worker-2    running", "worker-2    exited"))["worker"] == 1


def test_healthy_pipeline_observes():
    p = ScriptedOperator()
    assert p.next_command([("system", SYSTEM), ("user", obs())]) == OBSERVE


def test_missing_replica_is_rebuilt_then_reported():
    p = ScriptedOperator()
    msgs = [("system", SYSTEM), ("user", obs(PS_MISSING_WORKER))]
    assert p.next_command(msgs) == "wrenchctl scale worker 2"
    msgs += [("assistant", reply("wrenchctl scale worker 2")), ("user", obs(PS_TEXT))]
    cmd = p.next_command(msgs)
    assert cmd.startswith("wrenchctl report_fault worker ")
    msgs += [("assistant", reply(cmd)), ("user", obs(PS_TEXT))]
    assert p.next_command(msgs) == OBSERVE
    # A rebuild still in progress: look again instead of scaling twice.
    msgs2 = [("system", SYSTEM), ("user", obs(PS_MISSING_GATEWAY)), ("assistant", reply("wrenchctl scale gateway 1"))]
    msgs2.append(("user", obs(PS_MISSING_GATEWAY)))
    assert p.next_command(msgs2) == "wrenchctl ps"


def test_collapse_reads_logs_then_repairs_the_right_hop():
    p = ScriptedOperator()
    msgs = [("system", SYSTEM), ("user", obs(metrics=METRICS_COLLAPSED))]
    assert p.next_command(msgs) == "wrenchctl logs worker --tail 20"
    pg_logs = format_feedback(0, "wrenchctl logs worker --tail 20", 0, "pg insert failed (OperationalError)", "", 5)
    msgs_pg = msgs + [
        ("assistant", reply("wrenchctl logs worker --tail 20")),
        ("user", obs(metrics=METRICS_COLLAPSED, feedback=pg_logs)),
    ]
    assert p.next_command(msgs_pg) == PG_REPAIR
    quiet_logs = format_feedback(0, "wrenchctl logs worker --tail 20", 0, "done=50\ndone=51", "", 5)
    msgs_redis = msgs + [
        ("assistant", reply("wrenchctl logs worker --tail 20")),
        ("user", obs(metrics=METRICS_COLLAPSED, feedback=quiet_logs)),
    ]
    assert p.next_command(msgs_redis) == REDIS_REPAIR
    msgs_redis += [("assistant", reply(REDIS_REPAIR)), ("user", obs(metrics=METRICS_COLLAPSED))]
    assert p.next_command(msgs_redis).startswith("wrenchctl report_fault worker ")


def test_registered_as_an_inspect_model():
    model = get_model("compose-scripted/operator")
    out = asyncio.run(
        model.generate([ChatMessageSystem(content=SYSTEM), ChatMessageUser(content=obs(PS_MISSING_WORKER))])
    )
    assert out.completion == "```sh\nwrenchctl scale worker 2\n```"
    noop = get_model("compose-scripted/noop")
    assert asyncio.run(noop.generate([ChatMessageUser(content=obs())])).completion == f"```sh\n{OBSERVE}\n```"
