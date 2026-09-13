"""HMAC job stamps. The key lives only in the admin project (loadgen, probe),
mounted as a Docker secret file the episode runner generates per run; the
factory carries the signature through as opaque data. Throughput counts only
rows whose signature verifies, so nothing inside the factory can mint
throughput by inserting rows."""

import hashlib
import hmac
from pathlib import Path


def read_secret(path: str) -> bytes:
    """A secret file's contents, stripped. Missing file = misconfiguration,
    never a default: a guessable key would let the factory mint throughput."""
    data = Path(path).read_bytes().strip()
    if len(data) < 16:
        raise RuntimeError(f"secret at {path} is too short ({len(data)} bytes)")
    return data


def sign(key: bytes, job_id: str) -> str:
    return hmac.new(key, job_id.encode(), hashlib.sha256).hexdigest()


def verify(key: bytes, job_id: str, sig: str | None) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign(key, job_id), sig)
