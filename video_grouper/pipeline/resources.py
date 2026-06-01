"""Process-wide resource scheduler for the pipeline.

A step declares the named contention tags it competes on via its ``resources``
classvar (e.g. ``("gpu",)``, ``("autocam_ui",)``). The runner, when handed a
:class:`ResourceManager`, wraps a step's ``run`` in
:meth:`ResourceManager.acquire` so that steps sharing a *capped* resource are
serialized to that resource's capacity, while steps with no shared resource (or
only uncapped resources) run freely in parallel.

Capacities are passed in as a plain dict (built from
``[PIPELINE].gpu_concurrency`` / ``ram_heavy_concurrency`` upstream). A resource
that has no finite capacity — either absent from the dict or mapped to a
non-positive number — is treated as *unbounded*: no semaphore is created and
acquiring it is a no-op. This keeps the common single-game case allocation-free.

Deadlock avoidance: a step that names several capped resources acquires them in
a single canonical order (sorted by name) so two steps requesting overlapping
resource sets can never each hold one and wait on the other.

This module imports only the stdlib so it stays cheap to load in every bundle.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class ResourceManager:
    """Registry of named :class:`asyncio.Semaphore`s with finite capacities.

    Construct from a ``{name: capacity}`` dict. Names mapped to a positive int
    are capped (serialized to that many concurrent holders); any other name —
    missing, ``None``, or a non-positive number — is unbounded.
    """

    def __init__(self, capacities: dict[str, int] | None = None):
        # Keep only finite, positive capacities; everything else is unbounded.
        self._capacities: dict[str, int] = {
            name: int(cap)
            for name, cap in (capacities or {}).items()
            if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0
        }
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    def is_capped(self, name: str) -> bool:
        """True if *name* has a finite capacity (and so is serialized)."""
        return name in self._capacities

    async def _semaphore_for(self, name: str) -> asyncio.Semaphore | None:
        """Return (lazily creating) the semaphore for *name*, or ``None`` if uncapped."""
        if name not in self._capacities:
            return None
        sem = self._semaphores.get(name)
        if sem is None:
            async with self._lock:
                sem = self._semaphores.get(name)
                if sem is None:
                    sem = asyncio.Semaphore(self._capacities[name])
                    self._semaphores[name] = sem
        return sem

    @asynccontextmanager
    async def acquire(self, resources: tuple[str, ...]):
        """Acquire every *capped* resource in *resources*, releasing on exit.

        Capped resources are acquired in sorted (canonical) order so concurrent
        acquisitions of overlapping sets can't deadlock. Uncapped names are
        skipped entirely. Always releases what it acquired, even on error.
        """
        # Deduplicate + canonical order; only capped resources need a semaphore.
        capped = sorted({r for r in resources if self.is_capped(r)})
        acquired: list[asyncio.Semaphore] = []
        try:
            for name in capped:
                sem = await self._semaphore_for(name)
                if sem is not None:
                    await sem.acquire()
                    acquired.append(sem)
            yield
        finally:
            # Release in reverse acquisition order.
            for sem in reversed(acquired):
                sem.release()


def build_resource_manager(
    gpu_concurrency: int = 1,
    ram_heavy_concurrency: int = 1,
    autocam_ui_concurrency: int = 1,
) -> ResourceManager:
    """Build the default :class:`ResourceManager` for the built-in resources.

    Mirrors the named resources the built-in steps declare: ``gpu`` (detect),
    ``ram_heavy`` (stitch_correct, render), ``autocam_ui`` (autocam). ``autocam``
    is intrinsically single-window so its capacity is fixed at 1.
    """
    return ResourceManager(
        {
            "gpu": gpu_concurrency,
            "ram_heavy": ram_heavy_concurrency,
            "autocam_ui": autocam_ui_concurrency,
        }
    )
