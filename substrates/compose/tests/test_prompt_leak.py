"""Nothing the agent is shown may reveal what will happen to the factory or
that an admin side exists: no fault kinds, seeds, ledger vocabulary, HMAC,
or admin service names in the system prompt, wrenchctl's help, or the ps /
metrics output formats. Mirrors the fork's
tests/wrench/test_disruption_task.py::test_registry_round_trip_never_leaks_disruption_info."""

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from wrench_compose.prompt import system_prompt
from wrenchctl import render
from wrenchctl.cli import build_parser

FORBIDDEN = [
    # what the ledger records
    "armed",
    "fired",
    "ledger",
    "seed",
    "manifest",
    # fault kinds
    "entity_destruction",
    "belt_cut",
    "resource_exhaustion",
    "adaptive_strike",
    "strike",
    "toxic",
    # how throughput is verified
    "hmac",
    "signature",
    # the admin side
    "admin",
    "probe",
    "loadgen",
    "chaos",
    "toxiproxy",
    "pghog",
    "hog",
    "docker.sock",
]

PS_SAMPLE = [
    {
        "name": "wrench-factory-0-gateway-1",
        "service": "gateway",
        "state": "running",
        "status": "running",
        "started_at": "2026-09-12T10:00:00.000000000Z",
    },
    {
        "name": "wrench-factory-0-worker-2",
        "service": "worker",
        "state": "exited",
        "status": "exited",
        "started_at": "2026-09-12T10:00:00.000000000Z",
    },
]
METRICS_SAMPLE = {
    "uptime_s": 95,
    "gateway": {"admitted": 760, "rejected": 0, "inflight": 1, "admitted_per_min": 480.0},
    "queue": {"length": 760, "pending": 2, "lag": 0},
    "workers": {"wrench-factory-0-worker-1": {"done": 380}, "wrench-factory-0-worker-2": {"done": 379}},
    "jobs_done": 759,
    "jobs_done_per_min": 478.0,
    "unreachable": [],
}


def all_help_text() -> str:
    p = build_parser()
    texts = [p.format_help()]
    for action in p._subparsers._group_actions:  # noqa: SLF001 - argparse has no public walk
        for name, sub in action.choices.items():
            texts.append(f"--- {name}\n" + sub.format_help())
            if sub._subparsers:  # noqa: SLF001
                for a2 in sub._subparsers._group_actions:  # noqa: SLF001
                    for n2, s2 in a2.choices.items():
                        texts.append(f"--- {name} {n2}\n" + s2.format_help())
    return "\n".join(texts)


def rendered_surface() -> dict[str, str]:
    return {
        "system_prompt": system_prompt(),
        "help": all_help_text(),
        "ps": render.ps(PS_SAMPLE),
        "metrics": render.metrics(METRICS_SAMPLE),
        "scale": render.scale({"created": ["wrench-factory-0-worker-2"], "removed": [], "running": ["a", "b"]}),
        "config": render.config(
            {"recreated": ["wrench-factory-0-worker-1"], "env": {"REDIS_URL": "redis://redis:6379/0"}}
        ),
        "restart": render.restart({"restarted": ["wrench-factory-0-gateway-1"]}),
        "report_fault": render.report_fault({"recorded": True, "tick": 61234}),
        "logs": render.logs({"wrench-factory-0-worker-1": "done=50\ndone=100\n"}),
    }


@pytest.mark.parametrize("name", list(rendered_surface()))
def test_agent_facing_text_never_leaks(name):
    text = rendered_surface()[name].lower()
    hits = [w for w in FORBIDDEN if w in text]
    assert not hits, f"{name} mentions {hits}"


def test_system_prompt_names_the_tool_and_the_objective():
    text = system_prompt(workers=2)
    assert "wrenchctl report_fault" in text
    assert "jobs per minute" in text
    assert "2 replicas" in text
    for verb in ("ps", "logs", "exec", "restart", "scale", "config", "metrics", "report_fault"):
        assert verb in text


def test_installed_wrenchctl_help_matches_the_rendered_help():
    """The binary in the venv (same package the sandbox image installs)."""
    r = subprocess.run([sys.executable, "-m", "wrenchctl", "--help"], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == build_parser().format_help().strip()


def test_wrenchctl_usage_errors_do_not_call_the_api(monkeypatch):
    from wrenchctl.cli import run

    monkeypatch.setenv("WRENCH_AGENT_API", "http://127.0.0.1:1")
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err), pytest.raises(SystemExit) as e:
        run(["config", "edit", "worker", "NOT_A_PAIR"])
    assert e.value.code == 2
    assert "KEY=VALUE" in err.getvalue()
    with redirect_stdout(io.StringIO()), redirect_stderr(err), pytest.raises(SystemExit) as e:
        run(["exec", "worker"])
    assert e.value.code == 2


def test_wrenchctl_reports_an_unreachable_api_as_an_error(monkeypatch):
    from wrenchctl.cli import run

    monkeypatch.setenv("WRENCH_AGENT_API", "http://127.0.0.1:1")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = run(["ps"])
    assert code == 1
    assert "unreachable" in err.getvalue()
    with redirect_stdout(out), redirect_stderr(err):
        code = run(["--json", "ps"])
    assert code == 1
    assert '"error"' in out.getvalue()
