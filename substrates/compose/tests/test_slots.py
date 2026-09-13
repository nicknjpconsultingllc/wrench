"""The slot pool hands out each slot once, blocks past its size, and holds
a per-slot flock so a second runner process cannot share a slot."""

import asyncio
import subprocess
import sys
import time

import pytest

from wrench_compose import slots

HOLDER = """
import fcntl, sys, time
fh = open(sys.argv[1], "a+")
fcntl.flock(fh, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(60)
"""


@pytest.fixture
def held_slot(tmp_path):
    """A second process holding slot 0's lock in ``tmp_path``."""
    path = slots.lock_path(0, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_acquire_release_and_blocking(tmp_path):
    async def run():
        pool = slots.SlotPool(2, lock_dir=tmp_path)
        a = await pool.acquire()
        b = await pool.acquire()
        assert {a, b} == {0, 1} and pool.available == 0
        assert slots.lock_path(a, tmp_path).exists() and slots.try_lock(slots.lock_path(a, tmp_path)) is None
        third = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        assert not third.done()
        await pool.release(a)
        assert await asyncio.wait_for(third, 1) == a
        with pytest.raises(RuntimeError):
            await pool.release(b)
            await pool.release(b)

    asyncio.run(run())


def test_released_slot_lock_is_free_for_another_process(tmp_path):
    async def run():
        pool = slots.SlotPool(1, lock_dir=tmp_path)
        slot = await pool.acquire()
        await pool.release(slot)
        fh = slots.try_lock(slots.lock_path(slot, tmp_path))
        assert fh is not None
        fh.close()

    asyncio.run(run())


def test_slot_held_by_another_process_is_skipped(tmp_path, held_slot):
    async def run():
        pool = slots.SlotPool(2, lock_dir=tmp_path, poll_s=0.05)
        assert await pool.acquire() == 1  # slot 0 belongs to the other process
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.acquire(), 0.3)

    asyncio.run(run())


def test_acquire_waits_for_the_other_process_to_let_go(tmp_path, held_slot):
    async def run():
        pool = slots.SlotPool(1, lock_dir=tmp_path, poll_s=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.acquire(), 0.3)
        assert pool.available == 1  # the timed-out acquire gave its semaphore count back
        held_slot.kill()
        held_slot.wait()
        t = time.monotonic()
        assert await asyncio.wait_for(pool.acquire(), 2) == 0
        assert time.monotonic() - t < 1.5

    asyncio.run(run())


def test_pool_is_sized_by_env(monkeypatch):
    monkeypatch.setenv(slots.SLOTS_ENV, "3")
    slots._POOL = None

    async def run():
        pool = await slots.slot_pool()
        assert pool.size == 3 and pool is await slots.slot_pool()

    asyncio.run(run())
    monkeypatch.delenv(slots.SLOTS_ENV)
    assert slots.configured_slots() == 1
