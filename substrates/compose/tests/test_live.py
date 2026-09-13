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
    "kind,seed", [("entity_destruction", 1), ("belt_cut", 1), ("resource_exhaustion", 1), ("adaptive_strike", 1)]
)
def test_floor_noop(kind, seed, tmp_path):
    r = run_episode(f"live_floor_{kind}", kind, seed, "noop", out_root=tmp_path)
    assert r["error"] is None, r["error"]
    assert r["scores"]["fires"][0]["tr"] <= 0.2
