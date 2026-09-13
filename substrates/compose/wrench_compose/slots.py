"""Compose slots: one running episode per slot.

A slot is the ``<slot>`` in ``10.232.<slot>.0/24`` (factory network) and
``10.231.<slot>.0/24`` (admin network), the compose project names
``wrench-factory-<slot>`` / ``wrench-admin-<slot>`` and the sandbox
container ``wrench-factory-<slot>-agent-1``. Two episodes on one slot
would share containers, so a driver acquires a slot for the whole
episode. ``WRENCH_COMPOSE_SLOTS`` (default 1) sizes the process-wide pool;
the Inspect solver and the verifiers environment both go through it, the
same way the Factorio drivers share one server pool.

Across processes the slot is an ``fcntl.flock`` on ``runs/.slot-<n>.lock``,
taken inside ``acquire`` and dropped with ``release`` (or by the kernel when
the process dies), so two runners started by hand cannot share a slot: a
slot another process holds is skipped, and when every slot is taken
elsewhere ``acquire`` polls until one frees up.
"""

import asyncio
import fcntl
import os
from pathlib import Path

SLOTS_ENV = "WRENCH_COMPOSE_SLOTS"
LOCK_DIR = Path(__file__).resolve().parent.parent / "runs"
LOCK_POLL_S = 1.0


def configured_slots() -> int:
    return max(1, int(os.environ.get(SLOTS_ENV, "1")))


def lock_path(slot: int, lock_dir: Path = LOCK_DIR) -> Path:
    return Path(lock_dir) / f".slot-{slot}.lock"


def try_lock(path: Path):
    """The open file holding an exclusive, non-blocking flock on ``path``,
    or None when another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


class SlotPool:
    def __init__(self, size: int, lock_dir: Path = LOCK_DIR, poll_s: float = LOCK_POLL_S):
        self.size = int(size)
        self.lock_dir = Path(lock_dir)
        self.poll_s = float(poll_s)
        self._free = list(range(self.size))
        self._locks: dict[int, object] = {}
        self._sem = asyncio.Semaphore(self.size)
        self._lock = asyncio.Lock()

    async def acquire(self) -> int:
        await self._sem.acquire()
        try:
            while True:
                async with self._lock:
                    for slot in list(self._free):
                        fh = try_lock(lock_path(slot, self.lock_dir))
                        if fh is not None:
                            self._free.remove(slot)
                            self._locks[slot] = fh
                            return slot
                await asyncio.sleep(self.poll_s)
        except BaseException:
            self._sem.release()
            raise

    async def release(self, slot: int) -> None:
        async with self._lock:
            if slot in self._free:
                raise RuntimeError(f"slot {slot} released twice")
            fh = self._locks.pop(slot, None)
            if fh is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
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
