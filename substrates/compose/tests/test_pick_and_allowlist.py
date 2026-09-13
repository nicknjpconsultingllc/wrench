from wrench_compose.allowlist import is_agent_visible, service_of
from wrench_compose.pick import load_bearing, pick_adaptive, pick_seeded, seeded_index
from wrench_compose.positions import position_of

CANDIDATES = ["wrench-factory-0-gateway-1", "wrench-factory-0-worker-1", "wrench-factory-0-worker-2"]


def test_seeded_index_matches_lua_lcg():
    # server.lua: x = (seed*1103515245+12345) % 2^31; (x % n) + 1  (1-based)
    assert seeded_index(11, 2) == ((11 * 1103515245 + 12345) % 2147483648) % 2
    assert seeded_index(0, 0) is None


def test_documented_seeds_hit_worker_and_gateway():
    assert pick_seeded(3, CANDIDATES) == "wrench-factory-0-worker-1"
    assert pick_seeded(1, CANDIDATES) == "wrench-factory-0-gateway-1"
    assert pick_seeded(3, list(reversed(CANDIDATES))) == "wrench-factory-0-worker-1"  # order-independent


def test_adaptive_picks_max_holder_and_breaks_ties_by_seed():
    assert pick_adaptive({"gateway": 0, "redis": 165, "worker:a": 2, "worker:b": 2}, 1) == "redis"
    tie = {"worker:a": 2.0, "worker:b": 2.0}
    assert pick_adaptive(tie, 1) in tie
    assert pick_adaptive(tie, 1) == pick_adaptive(tie, 1)
    assert pick_adaptive({"gateway": 0.0}, 1) is None
    assert pick_adaptive({}, 1) is None


def test_allowlist_hides_admin_and_foreign_projects():
    factory = {"wrench.role": "factory", "com.docker.compose.project": "wrench-factory-0", "wrench.service": "worker"}
    admin = {"wrench.role": "admin", "com.docker.compose.project": "wrench-admin-0"}
    hog = {
        "wrench.role": "admin",
        "com.docker.compose.project": "wrench-admin-0",
        "com.docker.compose.service": "pghog",
    }
    other = {"wrench.role": "factory", "com.docker.compose.project": "wrench-factory-1"}
    assert is_agent_visible(factory, "wrench-factory-0")
    assert not is_agent_visible(admin, "wrench-factory-0")
    assert not is_agent_visible(hog, "wrench-factory-0")
    assert not is_agent_visible(other, "wrench-factory-0")
    assert not is_agent_visible({}, "wrench-factory-0")
    assert service_of(factory) == "worker"
    assert service_of(hog) == "pghog"


def test_positions_separate_services_beyond_loose_radius():
    names = ["gateway", "redis", "worker", "postgres", "netproxy"]
    for a in names:
        for b in names:
            if a != b:
                (ax, ay), (bx, by) = position_of(a), position_of(b)
                assert (ax - bx) ** 2 + (ay - by) ** 2 > 10.0**2


def test_load_bearing_prefers_single_points_of_failure_over_replicated_workers():
    flow = {"gateway": 80.0, "redis": 80.0, "worker:a": 42.0, "worker:b": 38.0}
    scores = load_bearing(flow, {"gateway": 1, "redis": 1, "worker": 2})
    assert scores["gateway"] == 1.0 and scores["redis"] == 1.0
    assert scores["worker:a"] < 0.3 and scores["worker:b"] < 0.3
    assert pick_adaptive(scores, 1) in ("gateway", "redis")
    # with a single worker it is a SPOF too and ties with the others
    single = load_bearing({"gateway": 80.0, "redis": 80.0, "worker:a": 80.0}, {"gateway": 1, "redis": 1, "worker": 1})
    assert set(single.values()) == {1.0}
    assert load_bearing({"gateway": 0.0}, {"gateway": 1}) == {"gateway": 0.0}
    assert load_bearing({"ghost:x": 5.0}, {"gateway": 1}) == {"ghost:x": 0.0}
