"""Live tests: need Docker, the wrench-svc:local image, and ~4 minutes each.
Run with `pytest -m compose_live`."""

import shutil

import pytest

from wrench_compose.episode import run_episode

pytestmark = pytest.mark.compose_live


@pytest.fixture(scope="module", autouse=True)
def _docker():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")


def test_entity_destruction_bracketing(tmp_path):
    noop = run_episode("live_noop", "entity_destruction", 3, "noop", out_root=tmp_path)
    assert noop["error"] is None, noop["error"]
    noop_tr = noop["scores"]["fires"][0]["tr"]
    assert 0.4 <= noop_tr <= 0.8, noop_tr  # one of two workers died
    oracle = run_episode("live_oracle", "entity_destruction", 3, "oracle", out_root=tmp_path)
    assert oracle["error"] is None, oracle["error"]
    oracle_tr = oracle["scores"]["fires"][0]["tr"]
    assert oracle_tr > noop_tr + 0.2
    assert oracle["scores"]["fires"][0]["recovered"] is True


@pytest.mark.parametrize(
    "kind,seed",
    [
        ("entity_destruction", 1),
        ("belt_cut", 1),
        ("resource_exhaustion", 1),
        ("adaptive_strike", 1),
        ("silent_throttle", 1),
    ],
)
def test_floor_noop(kind, seed, tmp_path):
    r = run_episode(f"live_floor_{kind}", kind, seed, "noop", out_root=tmp_path)
    assert r["error"] is None, r["error"]
    assert r["scores"]["fires"][0]["tr"] <= 0.2


def test_silent_throttle_is_invisible_in_ps_and_brackets(tmp_path):
    """The kind's contract: every container stays up and healthy in `ps` while
    throughput collapses (a) so a status-only agent files no report (recall 0),
    the no-op loses the throughput (b), and the oracle's one UPDATE brings it
    back (c)."""
    # (a) + status-only detection: ps shows all five services running at the
    # fire, and the ps-only agent reports nothing.
    st = run_episode("live_st_status_only", "silent_throttle", 1, "status_only", out_root=tmp_path)
    assert st["error"] is None, st["error"]
    snap = next(a for a in st["agent_actions"] if a.get("action") == "ps_snapshot")
    running = snap["running_by_service"]
    assert running.get("gateway") == 1 and running.get("redis") == 1 and running.get("postgres") == 1
    assert running.get("worker") == 2  # every box up during the fault
    assert not any(a.get("action") == "report_fault" for a in st["agent_actions"])
    assert st["scores"]["detection"]["recall"] == 0.0  # blind to a fault that kills no box

    # (b) floor: the no-op loses at least 80% of throughput.
    noop = run_episode("live_st_noop", "silent_throttle", 1, "noop", out_root=tmp_path)
    assert noop["error"] is None, noop["error"]
    assert noop["scores"]["fires"][0]["tr"] <= 0.2

    # (c) bracket: the oracle clears the delay and recovers.
    oracle = run_episode("live_st_oracle", "silent_throttle", 1, "oracle", out_root=tmp_path)
    assert oracle["error"] is None, oracle["error"]
    assert oracle["scores"]["fires"][0]["tr"] >= 0.8
    assert oracle["scores"]["fires"][0]["recovered"] is True
    assert oracle["scores"]["detection"]["recall"] == 1.0


def test_silent_throttle_restart_all_stays_failed(tmp_path):
    """A blind bounce must not clear it: the trigger survives a postgres
    restart (data lives in the container filesystem)."""
    r = run_episode("live_st_restart_all", "silent_throttle", 1, "restart_all", out_root=tmp_path)
    assert r["error"] is None, r["error"]
    assert r["scores"]["fires"][0]["tr"] <= 0.2
