"""Minimal JSON HTTP server/client on the stdlib (no framework).

`python -m wrench_compose.httpjson METHOD URL [BODY_JSON]` makes one request
from inside a container; the episode runner uses it through `docker exec` to
reach the probe, which binds only to the admin network and publishes no
port."""

import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Handler = Callable[[dict, dict], tuple[int, object]]  # (query, body) -> (status, payload)


class HttpError(Exception):
    def __init__(self, status: int, payload: object):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


def serve(
    port: int, routes: dict[tuple[str, str], Handler], quiet: bool = True, host: str = "0.0.0.0"
) -> ThreadingHTTPServer:
    """Start a threaded server in the background. routes: {(METHOD, path): handler}.
    `host` is the bind address: a container on two networks binds each
    surface to the IP of the network that may see it."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            if not quiet:
                super().log_message(*a)

        def _dispatch(self, method):
            u = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            h = routes.get((method, u.path))
            if h is None:
                return self._send(404, {"error": f"no route {method} {u.path}"})
            try:
                status, payload = h(query, body)
            except HttpError as e:
                status, payload = e.status, {"error": e.payload}
            except Exception as e:  # noqa: BLE001 - surface to caller
                status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
            self._send(status, payload)

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def request(method: str, url: str, body: object | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = raw.decode(errors="replace")
        raise HttpError(e.code, payload.get("error", payload) if isinstance(payload, dict) else payload) from None


def get(url, timeout=10.0):
    return request("GET", url, None, timeout)


def post(url, body=None, timeout=30.0):
    return request("POST", url, body or {}, timeout)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    method, url = argv[0], argv[1]
    body = json.loads(argv[2]) if len(argv) > 2 else None
    try:
        payload = request(method, url, body, timeout=float(argv[3]) if len(argv) > 3 else 30.0)
    except HttpError as e:
        print(json.dumps({"error": e.payload}))
        return e.status // 100 if e.status >= 100 else 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
