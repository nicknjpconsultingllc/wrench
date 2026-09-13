from wrench_compose.signing import sign, verify

KEY = b"admin-only"


def test_roundtrip():
    sig = sign(KEY, "job-1")
    assert verify(KEY, "job-1", sig)


def test_wrong_key_or_id_or_missing_sig_rejected():
    sig = sign(KEY, "job-1")
    assert not verify(b"factory-guess", "job-1", sig)
    assert not verify(KEY, "job-2", sig)
    assert not verify(KEY, "job-1", None)
    assert not verify(KEY, "job-1", "")
    assert not verify(KEY, "job-1", sig[:-1] + ("0" if sig[-1] != "0" else "1"))


def test_probe_counts_only_verified_rows():
    """Rows the factory mints itself (no key) never count as throughput."""
    rows = [("a", sign(KEY, "a")), ("b", sign(b"stolen?", "b")), ("c", "deadbeef"), ("d", sign(KEY, "d"))]
    assert sum(verify(KEY, j, s) for j, s in rows) == 2
