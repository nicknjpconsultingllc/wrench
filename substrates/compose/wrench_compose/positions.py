"""Synthetic positions so the fork's position-radius detection scorer works
unchanged: services sit 20 units apart, so a report on the right service is
inside the strict 3-unit radius and a report on the wrong one is outside the
loose 10-unit radius."""

SERVICE_POSITIONS: dict[str, tuple[float, float]] = {
    "gateway": (0.0, 0.0),
    "redis": (20.0, 0.0),
    "worker": (40.0, 0.0),
    "postgres": (60.0, 0.0),
    "netproxy": (80.0, 0.0),
}


def position_of(service: str) -> tuple[float, float]:
    return SERVICE_POSITIONS.get(service, (-100.0, -100.0))
