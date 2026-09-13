"""Fired-event manifests through ``wrench_core``'s detection scorer with the
compose config: which ``report_fault <service>`` earns credit."""

import pytest
from wrench_core.scoring import detection_metrics

from wrench_compose.manifest import BELT_CUT_PATH, belt_cut_affected, service_entry
from wrench_compose.positions import SERVICE_POSITIONS, position_of
from wrench_compose.scoring import COMPOSE_CONFIG

TOXIC = {
    "name": "belt_cut",
    "type": "latency",
    "stream": "downstream",
    "toxicity": 1.0,
    "attributes": {"latency": 1000},
}


def report(tick, service):
    x, y = position_of(service)
    return {"tick": tick, "event": "report_fault", "detail": {"service": service, "cause": "test", "x": x, "y": y}}


def belt_cut_fire():
    workers = [
        service_entry("worker", f"wrench-factory-0-worker-{n}", via="netproxy", toxic="latency", same_type_total=2)
        for n in (1, 2)
    ]
    return {
        "tick": 60000,
        "event": "fired",
        "kind": "belt_cut",
        "seed": 1,
        "affected": belt_cut_affected(workers, TOXIC),
    }


def test_path_services_have_positions():
    assert set(BELT_CUT_PATH) <= set(SERVICE_POSITIONS)
    assert position_of("netproxy") != position_of("redis") != position_of("worker")


def test_belt_cut_manifest_keeps_workers_first_and_adds_the_path():
    fire = belt_cut_fire()
    workers_only = dict(fire, affected=fire["affected"][:2])  # the manifest before this change
    assert (
        detection_metrics([workers_only, report(66000, "netproxy")], [workers_only], config=COMPOSE_CONFIG)["recall"]
        == 0.0
    )
    assert [e["service"] for e in fire["affected"]] == ["worker", "worker", "netproxy", "redis"]
    assert fire["affected"][0]["same_type_total"] == 2
    for entry in fire["affected"][2:]:
        assert entry["container"] is None and entry["via"] == "netproxy" and entry["toxic"] == "latency"
        assert entry["toxic_attributes"] == {"latency": 1000}


@pytest.mark.parametrize("service", ["worker", "netproxy", "redis"])
def test_report_on_a_belt_cut_path_service_matches_strict(service):
    fire = belt_cut_fire()
    det = detection_metrics([fire, report(66000, service)], [fire], config=COMPOSE_CONFIG)
    assert det["precision_strict"] == 1.0 and det["precision"] == 1.0 and det["recall"] == 1.0
    assert det["latencies"] == [6000]


@pytest.mark.parametrize("service", ["gateway", "postgres"])
def test_report_off_the_path_earns_nothing(service):
    fire = belt_cut_fire()
    det = detection_metrics([fire, report(66000, service)], [fire], config=COMPOSE_CONFIG)
    assert det["precision_strict"] == 0.0 and det["precision"] == 0.0 and det["recall"] == 0.0
