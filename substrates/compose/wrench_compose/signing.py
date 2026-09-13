"""HMAC job stamps. The key lives only in the admin project (loadgen, probe);
the factory carries the signature through as opaque data. Throughput counts
only rows whose signature verifies, so nothing inside the factory can mint
throughput by inserting rows."""

import hashlib
import hmac


def sign(key: bytes, job_id: str) -> str:
    return hmac.new(key, job_id.encode(), hashlib.sha256).hexdigest()


def verify(key: bytes, job_id: str, sig: str | None) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign(key, job_id), sig)
