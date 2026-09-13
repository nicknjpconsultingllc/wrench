"""The slot pool hands out each slot once and blocks past its size."""

import asyncio

import pytest

from wrench_compose import slots


def test_acquire_release_and_blocking():
    async def run():
        pool = slots.SlotPool(2)
        a = await pool.acquire()
        b = await pool.acquire()
        assert {a, b} == {0, 1} and pool.available == 0
        third = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        assert not third.done()
        await pool.release(a)
        assert await asyncio.wait_for(third, 1) == a
        with pytest.raises(RuntimeError):
            await pool.release(b)
            await pool.release(b)

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
