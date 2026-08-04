"""Regression: the NTFY response listener must survive server disconnects.

ntfy.sh serves the topic as a long-lived chunked stream and closes it
periodically -- observed every 1.5-3 hours in production. The subscribe
coroutine was spawned exactly once, and its outer `except` logged the
error and returned, so the first close ended the task for good. From
then on the service silently received nothing: game start/end
confirmations and team-info replies were dropped until someone
restarted the service.

Observed 2026-08-04: subscribed 01:18, dropped 02:57, dead 25 minutes
until an unrelated restart at 03:22, dropped again 06:16.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from video_grouper.api_integrations.ntfy_response import NtfyResponseService


def _service() -> NtfyResponseService:
    svc = NtfyResponseService.__new__(NtfyResponseService)
    svc.topic = "soccer-cam-test"
    svc.server_url = "https://ntfy.sh"
    # Keep the test fast; the production values are 5s/300s.
    svc._RECONNECT_MIN_SECONDS = 0
    svc._RECONNECT_MAX_SECONDS = 0
    return svc


@pytest.mark.asyncio
async def test_reconnects_after_peer_closes_the_stream():
    """The exact production failure: httpx raises when ntfy.sh hangs up."""
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        raise RuntimeError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )

    svc = _service()
    with patch.object(svc, "_subscribe_to_real_ntfy", flaky):
        task = asyncio.create_task(svc._real_ntfy_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls > 1, (
        f"listener resubscribed {calls} time(s); a dropped stream must be "
        f"retried, not left dead until a service restart"
    )


@pytest.mark.asyncio
async def test_reconnects_after_a_clean_stream_end():
    """A stream that ends without raising also means 'no longer subscribed'."""
    calls = 0

    async def ends_cleanly():
        nonlocal calls
        calls += 1

    svc = _service()
    with patch.object(svc, "_subscribe_to_real_ntfy", ends_cleanly):
        task = asyncio.create_task(svc._real_ntfy_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls > 1


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed():
    """Shutdown must actually stop the loop, not be treated as a drop."""

    async def hang():
        await asyncio.sleep(3600)

    svc = _service()
    with patch.object(svc, "_subscribe_to_real_ntfy", hang):
        task = asyncio.create_task(svc._real_ntfy_forever())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled() or task.done()


def test_subscribe_propagates_errors_to_the_reconnect_loop():
    """Static guard: the outer handler must re-raise. Swallowing it is
    what left the listener dead in the first place."""
    import inspect

    src = inspect.getsource(NtfyResponseService._subscribe_to_real_ntfy)
    tail = src[src.rindex("except Exception") :]
    assert "raise" in tail, (
        "_subscribe_to_real_ntfy swallows its final exception; "
        "_real_ntfy_forever can then never distinguish a drop and will "
        "not reconnect"
    )
