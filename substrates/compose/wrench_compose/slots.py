"""Compose slots: one running episode per slot.

A slot is the ``<slot>`` in ``10.232.<slot>.0/24`` (factory network) and
``10.231.<slot>.0/24`` (admin network), the compose project names
``wrench-factory-<slot>`` / ``wrench-admin-<slot>`` and the sandbox
container ``wrench-factory-<slot>-agent-1``. Two episodes on one slot
would share containers, so a driver acquires a slot for the whole
episode. ``WRENCH_COMPOSE_SLOTS`` (default 1) sizes the process-wide pool;
the Inspect solver and the verifiers environment both go through it, the
same way the Factorio drivers share one server pool.
"""

import asyncio
import os

SLOTS_ENV = "WRENCH_COMPOSE_SLOTS"


def configured_slots() -> int:
    return max(1, int(os.environ.get(SLOTS_ENV, "1")))


class SlotPool:
    def __init__(self, size: int):
        self.size = int(size)
        self._free = list(range(self.size))
        self._sem = asyncio.Semaphore(self.size)
        self._lock = asyncio.Lock()

    async def acquire(self) -> int:
        await self._sem.acquire()
        async with self._lock:
            return self._free.pop(0)

    async def release(self, slot: int) -> None:
        async with self._lock:
            if slot in self._free:
                raise RuntimeError(f"slot {slot} released twice")
            self._free.append(slot)
            self._free.sort()
        self._sem.release()

    @property
    def available(self) -> int:
        return len(self._free)


_POOL: SlotPool | None = None
_POOL_LOOP = None


async def slot_pool() -> SlotPool:
    """The process-wide pool, sized by ``WRENCH_COMPOSE_SLOTS``. Rebuilt if
    the event loop changed (Inspect runs each eval in its own loop)."""
    global _POOL, _POOL_LOOP
    loop = asyncio.get_running_loop()
    if _POOL is None or _POOL_LOOP is not loop:
        _POOL = SlotPool(configured_slots())
        _POOL_LOOP = loop
    return _POOL
