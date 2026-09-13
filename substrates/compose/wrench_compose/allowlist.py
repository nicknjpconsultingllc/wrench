"""Agent-surface allowlist: a container is agent-visible only when it carries
wrench.role=factory AND belongs to the factory compose project. Admin
containers (probe, chaos, loadgen, toxiproxy, pghog) never match."""

ROLE_LABEL = "wrench.role"
SERVICE_LABEL = "wrench.service"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


def is_agent_visible(labels: dict, factory_project: str) -> bool:
    labels = labels or {}
    return labels.get(ROLE_LABEL) == "factory" and labels.get(COMPOSE_PROJECT_LABEL) == factory_project


def service_of(labels: dict) -> str | None:
    labels = labels or {}
    return labels.get(SERVICE_LABEL) or labels.get(COMPOSE_SERVICE_LABEL)
