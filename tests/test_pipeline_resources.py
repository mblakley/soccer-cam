"""Tests for the pipeline ResourceManager (serialization + deadlock-freedom)."""

from __future__ import annotations

import asyncio

import pytest

from video_grouper.pipeline.resources import ResourceManager, build_resource_manager


async def _hold(
    rm: ResourceManager,
    resources: tuple[str, ...],
    order: list[str],
    tag: str,
    enter_barrier: asyncio.Event | None = None,
    release_barrier: asyncio.Event | None = None,
):
    """Acquire *resources*, record enter/exit in *order*, optionally barrier-gated."""
    async with rm.acquire(resources):
        order.append(f"enter:{tag}")
        if enter_barrier is not None:
            enter_barrier.set()
        if release_barrier is not None:
            await release_barrier.wait()
        order.append(f"exit:{tag}")


@pytest.mark.asyncio
async def test_capped_resource_serializes():
    """Two acquirers of a capacity-1 resource must run one-at-a-time."""
    rm = ResourceManager({"gpu": 1})
    order: list[str] = []
    a_in = asyncio.Event()
    a_release = asyncio.Event()

    # Task A enters and holds until told to release.
    task_a = asyncio.create_task(
        _hold(rm, ("gpu",), order, "a", enter_barrier=a_in, release_barrier=a_release)
    )
    await a_in.wait()  # A is now holding the gpu semaphore

    # Task B tries to enter; it must block until A releases.
    task_b = asyncio.create_task(_hold(rm, ("gpu",), order, "b"))
    await asyncio.sleep(0.02)
    assert order == ["enter:a"], "B must not enter while A holds the capped gpu"

    a_release.set()
    await asyncio.gather(task_a, task_b)
    # A fully exits before B enters — strict serialization (B then runs to
    # completion since it has no release barrier of its own).
    assert order == ["enter:a", "exit:a", "enter:b", "exit:b"]


@pytest.mark.asyncio
async def test_independent_resources_run_concurrently():
    """Distinct capped resources do not block each other."""
    rm = ResourceManager({"gpu": 1, "ram_heavy": 1})
    order: list[str] = []
    a_in = asyncio.Event()
    b_in = asyncio.Event()
    release = asyncio.Event()

    task_a = asyncio.create_task(
        _hold(rm, ("gpu",), order, "a", enter_barrier=a_in, release_barrier=release)
    )
    task_b = asyncio.create_task(
        _hold(
            rm, ("ram_heavy",), order, "b", enter_barrier=b_in, release_barrier=release
        )
    )
    await asyncio.wait_for(asyncio.gather(a_in.wait(), b_in.wait()), timeout=1.0)
    # Both entered without either releasing -> they ran concurrently.
    assert set(order) == {"enter:a", "enter:b"}
    release.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.asyncio
async def test_uncapped_names_run_concurrently():
    """A name with no finite capacity is unbounded — never serializes."""
    rm = ResourceManager({"gpu": 1})  # 'autocam_ui' deliberately uncapped here
    order: list[str] = []
    a_in = asyncio.Event()
    b_in = asyncio.Event()
    release = asyncio.Event()

    task_a = asyncio.create_task(
        _hold(
            rm, ("autocam_ui",), order, "a", enter_barrier=a_in, release_barrier=release
        )
    )
    task_b = asyncio.create_task(
        _hold(
            rm, ("autocam_ui",), order, "b", enter_barrier=b_in, release_barrier=release
        )
    )
    await asyncio.wait_for(asyncio.gather(a_in.wait(), b_in.wait()), timeout=1.0)
    assert set(order) == {"enter:a", "enter:b"}
    release.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.asyncio
async def test_nonpositive_capacity_is_unbounded():
    """Zero / negative capacities are treated as unbounded (not a 0-permit lock)."""
    rm = ResourceManager({"gpu": 0, "ram_heavy": -1})
    assert not rm.is_capped("gpu")
    assert not rm.is_capped("ram_heavy")
    # Two acquirers must both proceed without blocking.
    order: list[str] = []
    a_in = asyncio.Event()
    b_in = asyncio.Event()
    release = asyncio.Event()
    task_a = asyncio.create_task(
        _hold(rm, ("gpu",), order, "a", enter_barrier=a_in, release_barrier=release)
    )
    task_b = asyncio.create_task(
        _hold(rm, ("gpu",), order, "b", enter_barrier=b_in, release_barrier=release)
    )
    await asyncio.wait_for(asyncio.gather(a_in.wait(), b_in.wait()), timeout=1.0)
    release.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.asyncio
async def test_multi_resource_acquire_is_deadlock_free():
    """Two tasks requesting overlapping capped sets can't deadlock.

    Task A wants (gpu, ram_heavy); task B wants (ram_heavy, gpu). Because both
    acquire in canonical (sorted) order, they can never each hold one and wait
    on the other. The whole thing completes within the timeout.
    """
    rm = ResourceManager({"gpu": 1, "ram_heavy": 1})
    order: list[str] = []

    async def both(resources, tag):
        async with rm.acquire(resources):
            order.append(f"enter:{tag}")
            await asyncio.sleep(0.01)
            order.append(f"exit:{tag}")

    await asyncio.wait_for(
        asyncio.gather(
            both(("gpu", "ram_heavy"), "a"),
            both(("ram_heavy", "gpu"), "b"),
        ),
        timeout=2.0,
    )
    # Serialized (both hold both capped resources), no interleave, no hang.
    assert order in (
        ["enter:a", "exit:a", "enter:b", "exit:b"],
        ["enter:b", "exit:b", "enter:a", "exit:a"],
    )


@pytest.mark.asyncio
async def test_empty_resources_is_noop():
    """Acquiring an empty tuple is a no-op that always proceeds."""
    rm = ResourceManager({"gpu": 1})
    async with rm.acquire(()):
        pass  # no exception, no blocking


@pytest.mark.asyncio
async def test_releases_on_exception():
    """A capped resource is released even when the body raises."""
    rm = ResourceManager({"gpu": 1})
    with pytest.raises(ValueError):
        async with rm.acquire(("gpu",)):
            raise ValueError("boom")
    # If it wasn't released, this second acquire would hang -> timeout failure.
    await asyncio.wait_for(rm.acquire(("gpu",)).__aenter__(), timeout=1.0)


def test_build_resource_manager_caps_builtins():
    rm = build_resource_manager(gpu_concurrency=2, ram_heavy_concurrency=3)
    assert rm.is_capped("gpu")
    assert rm.is_capped("ram_heavy")
    assert rm.is_capped("autocam_ui")
