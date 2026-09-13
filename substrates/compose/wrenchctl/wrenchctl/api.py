"""HTTP client for the agent API. Raises ApiError with the server's message."""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode

DEFAULT_API = "http://wrenchapi:8081"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def base_url() -> str:
    return os.environ.get("WRENCH_AGENT_API", DEFAULT_API).rstrip("/")


def call(method: str, path: str, query: dict | None = None, body: dict | None = None, timeout: float = 120.0):
    url = base_url() + path
    if query:
        url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("error", raw.decode(errors="replace"))
        except Exception:  # noqa: BLE001
            msg = raw.decode(errors="replace")
        raise ApiError(e.code, str(msg)) from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(0, f"agent api unreachable at {base_url()}: {e}") from None
    return json.loads(raw) if raw else None


def ps():
    return call("GET", "/agent/ps")


def logs(service: str, tail: int = 50):
    return call("GET", "/agent/logs", query={"service": service, "tail": tail})


def exec_(service: str, cmd: list[str]):
    return call("POST", "/agent/exec", body={"service": service, "cmd": cmd})


def restart(service: str):
    return call("POST", "/agent/restart", body={"service": service})


def scale(service: str, replicas: int):
    return call("POST", "/agent/scale", body={"service": service, "replicas": replicas})


def config_edit(service: str, env: dict[str, str]):
    return call("POST", "/agent/config", body={"service": service, "env": env})


def metrics():
    return call("GET", "/agent/metrics")


def report_fault(service: str, cause: str):
    return call("POST", "/agent/report_fault", body={"service": service, "cause": cause})
