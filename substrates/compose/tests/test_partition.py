"""Anti-gaming tests from inside the sandbox (compose_live, ~1 minute).

Brings both projects up once with no armed spec and asserts, from the agent
container's point of view, that the admin side does not exist: no route to
admin routes, Toxiproxy's control API, the probe or loadgen; `wrenchctl`
refuses admin containers; the scale cap holds; rows minted in postgres do not
count; loadgen cannot be restarted; no docker socket, secrets or root."""

import json
import shutil
import subprocess
import time

import pytest

from wrench_compose.agents import AgentError, Wrenchctl
from wrench_compose.episode import (
    ProbeClient,
    agent_container,
    compose,
    slot_env,
    teardown,
    wait_agent_surface,
    write_secrets,
)

pytestmark = pytest.mark.compose_live
SLOT = 0
CFG = {"workers": 2, "rps": 8.0, "seed": 1, "work_iters": 1000000, "sample_ms": 500}
ADMIN_SERVICES = ["probe", "loadgen", "chaos", "toxiproxy", "pghog", "agent"]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    secrets_dir = tmp_path_factory.mktemp("secrets")
    env = slot_env(SLOT, CFG, secrets_dir)
    teardown(env, SLOT)
    write_secrets(secrets_dir)
    ctl = Wrenchctl(agent_container(SLOT))
    try:
        compose(env, "factory.yml", "up", "-d", "--wait", "--quiet-pull", capture=True)
        compose(env, "admin.yml", "up", "-d", "--quiet-pull", capture=True)
        probe = ProbeClient(SLOT)
        probe.wait()
        wait_agent_surface(ctl)
        yield {"env": env, "ctl": ctl, "probe": probe, "hmac_key": (secrets_dir / "hmac.key").read_text().strip()}
    finally:
        teardown(env, SLOT, secrets_dir)


def in_sandbox(*argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", agent_container(SLOT), *argv], capture_output=True, text=True, timeout=timeout
    )


CONNECT = """
import socket, sys, json
out = {}
for host, port in json.loads(sys.argv[1]):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); out[f"{host}:{port}"] = "connected"
    except Exception as e:
        out[f"{host}:{port}"] = type(e).__name__
    finally:
        s.close()
print(json.dumps(out))
"""


def test_nothing_publishes_a_port(stack):
    """A published port is reachable from every bridge (masqueraded to the
    gateway address) and via host.docker.internal, so the guarantee behind
    the network-level tests is structural: no wrench container has one."""
    r = subprocess.run(
        ["docker", "ps", "--filter", "name=wrench-", "--format", "{{.Names}}\t{{.Ports}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = [line.split("\t") for line in r.stdout.splitlines() if line.strip()]
    assert len(rows) >= 9, rows
    published = [name for name, ports in rows if "->" in ports]
    assert not published, published


def test_admin_side_unreachable_at_network_level(stack):
    slot = SLOT
    targets = [
        ["probe", 8080],
        ["toxiproxy", 8474],
        ["netproxy", 8474],
        ["loadgen", 8000],
        ["wrenchapi", 8080],  # chaos's admin listener is bound to the admin network address only
        [f"10.231.{slot}.20", 8080],  # chaos admin, admin network: no route
        [f"10.231.{slot}.11", 8080],  # probe, admin network
        [f"10.231.{slot}.10", 8474],  # toxiproxy control, admin network
    ]
    r = in_sandbox("python", "-c", CONNECT, json.dumps(targets))
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    connected = {k: v for k, v in res.items() if v == "connected"}
    assert not connected, res
    # the agent listener itself is reachable, but admin routes are not on it
    r = in_sandbox(
        "python",
        "-c",
        "import urllib.request,urllib.error\n"
        "try:\n"
        "  urllib.request.urlopen(urllib.request.Request('http://wrenchapi:8081/chaos/fire',data=b'{}',method='POST'),timeout=3)\n"
        "except urllib.error.HTTPError as e:\n"
        "  print(e.code)\n",
    )
    assert r.stdout.strip() == "404", (r.stdout, r.stderr)
    # the belt itself (netproxy:6379) stays reachable: it is part of the factory
    r = in_sandbox("python", "-c", CONNECT, json.dumps([["netproxy", 6379], ["wrenchapi", 8081]]))
    assert set(json.loads(r.stdout).values()) == {"connected"}, r.stdout


def test_wrenchctl_refuses_non_factory_containers(stack):
    ctl = stack["ctl"]
    listed = {c["service"] for c in ctl.ps()}
    assert listed == {"gateway", "redis", "worker", "postgres"}, listed
    for svc in ADMIN_SERVICES:
        with pytest.raises(AgentError, match="unknown factory service"):
            ctl.exec(svc, ["id"])
        with pytest.raises(AgentError, match="unknown factory service"):
            ctl.logs(svc)


def test_scale_is_capped_at_four(stack):
    ctl = stack["ctl"]
    with pytest.raises(AgentError, match="replicas must be 0..4"):
        ctl.scale("worker", 100)
    with pytest.raises(AgentError, match="replicas must be 0..4"):
        ctl.scale("worker", -1)
    assert len([c for c in ctl.ps() if c["service"] == "worker"]) == 2
    try:
        res = ctl.scale("worker", 4)
        assert len(res["running"]) == 4 and len(res["created"]) == 2
    finally:
        ctl.scale("worker", 2)
    assert len([c for c in ctl.ps() if c["service"] == "worker" and c["state"] == "running"]) == 2


def test_rows_minted_in_postgres_do_not_count(stack):
    ctl, probe = stack["ctl"], stack["probe"]
    n = 1000
    before = probe.get("/status")
    t0 = time.monotonic()
    res = ctl.exec(
        "postgres",
        [
            "psql",
            "-U",
            "postgres",
            "-d",
            "factory",
            "-c",
            f"INSERT INTO jobs_done (job_id, sig, worker) SELECT 'minted-' || g, 'forged', 'agent' FROM generate_series(1, {n}) g",
        ],
    )
    assert res["exit_code"] == 0, res
    time.sleep(2.5)  # > 2 probe samples
    after = probe.get("/status")
    elapsed = time.monotonic() - t0
    assert after["rejected"] - before["rejected"] == n, (before, after)
    # legitimate throughput is 8 jobs/s; the minted rows never enter the verified count
    assert after["jobs_done"] - before["jobs_done"] <= 8 * elapsed + 16, (before, after, elapsed)


def test_loadgen_cannot_be_restarted_or_stopped(stack):
    ctl, env = stack["ctl"], stack["env"]
    for fn in (
        lambda: ctl.restart("loadgen"),
        lambda: ctl.scale("loadgen", 0),
        lambda: ctl.config("loadgen", {"RPS": "0"}),
    ):
        with pytest.raises(AgentError, match="unknown factory service"):
            fn()
    ps = compose(env, "admin.yml", "ps", "--format", "json", check=True, capture=True).stdout
    states = {json.loads(line)["Service"]: json.loads(line)["State"] for line in ps.splitlines() if line.strip()}
    assert states.get("loadgen") == "running", states


def test_sandbox_has_no_socket_no_secrets_no_root(stack):
    checks = {
        "socket": ["test", "!", "-e", "/var/run/docker.sock"],
        "secrets": ["test", "!", "-e", "/run/secrets"],
        "no_docker_cli": ["sh", "-c", "! command -v docker"],
        "readonly_root": ["sh", "-c", "! touch /probe_write 2>/dev/null && ! touch /home/agent/x 2>/dev/null"],
    }
    for name, argv in checks.items():
        r = in_sandbox(*argv)
        assert r.returncode == 0, (name, r.stdout, r.stderr)
    r = in_sandbox("id", "-u")
    assert r.stdout.strip() == "10001"


def test_hmac_key_absent_from_every_factory_container(stack):
    ctl, key = stack["ctl"], stack["hmac_key"]
    for svc in ("gateway", "redis", "worker", "postgres"):
        env = ctl.exec(svc, ["env"])
        assert key not in env["stdout"], svc
        assert "HMAC" not in env["stdout"], svc
        ls = ctl.exec(svc, ["sh", "-c", "ls /run/secrets 2>&1 || true"])
        assert "hmac" not in ls["stdout"], (svc, ls)
    # the admin listener never leaks through the sandbox's own environment either
    r = in_sandbox("env")
    assert key not in r.stdout and "HMAC" not in r.stdout
    # and the probe still verifies (the key it holds is the one loadgen signs with)
    st = stack["probe"].get("/status")
    assert st["jobs_done"] > 0 and st["pg_errors"] == 0


def test_metrics_is_the_factory_view(stack):
    m = stack["ctl"].metrics()
    assert set(m) >= {"gateway", "queue", "workers", "jobs_done", "jobs_done_per_min"}
    assert "probe" not in json.dumps(m)
    assert len(m["workers"]) == 2
    time.sleep(31)
    m = stack["ctl"].metrics()
    assert m["jobs_done_per_min"] is not None and m["jobs_done_per_min"] > 300, m
