"""Tests for NTFY topic-replay dedup via the consumed-message-id ledger.

The listener reconnects with ``?since=24h``, which redelivers every recent
topic message with its stable server-assigned id. Without a ledger of what we
already consumed, a replayed "Yes"/"No" tap can re-answer a freshly-queued
question on its own — the regression these tests pin down (2026-06 incident
where an earlier game's taps walked a new game's game-start question forward).

The NTFY topic is the source of truth; the ledger only records what we used
from it.
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.task_processors.services.ntfy_service import (
    NTFY_PROCESSED_LOG_RETENTION_SECONDS,
    NtfyService,
)
from video_grouper.utils.config import NtfyConfig
from video_grouper.utils.paths import get_ntfy_processed_log_path


@pytest.fixture
def config():
    return NtfyConfig(enabled=True, topic="test-topic")


@pytest.fixture
def storage_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


def test_record_and_has_used_message(config, storage_path):
    svc = NtfyService(config, storage_path)

    assert not svc.has_used_message("abc")
    svc.record_used_message("abc", event="message", message="Yes, game started")
    assert svc.has_used_message("abc")
    assert not svc.has_used_message("other-id")

    # A falsy id is never "used" and recording it is a no-op.
    assert not svc.has_used_message(None)
    svc.record_used_message(None)

    # Recording the same id twice does not duplicate the audit-log line.
    svc.record_used_message("abc")
    with open(get_ntfy_processed_log_path(storage_path)) as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["id"] == "abc"
    assert entry["message"] == "Yes, game started"


def test_used_messages_persist_across_restart(config, storage_path):
    svc1 = NtfyService(config, storage_path)
    svc1.record_used_message("persist-me", message="No, not yet at 05:00")

    # A fresh service (e.g. after a service/tray restart) reloads the ledger,
    # so the replay guard survives the restart that ``?since=24h`` exists for.
    svc2 = NtfyService(config, storage_path)
    assert svc2.has_used_message("persist-me")


def test_stale_used_messages_pruned_on_load(config, storage_path):
    log_path = get_ntfy_processed_log_path(storage_path)
    stale_at = (
        datetime.now() - timedelta(seconds=NTFY_PROCESSED_LOG_RETENTION_SECONDS + 3600)
    ).isoformat()
    fresh_at = datetime.now().isoformat()
    with open(log_path, "w") as f:
        f.write(json.dumps({"id": "stale", "recorded_at": stale_at}) + "\n")
        f.write(json.dumps({"id": "recent", "recorded_at": fresh_at}) + "\n")

    svc = NtfyService(config, storage_path)
    assert not svc.has_used_message("stale")  # older than retention -> dropped
    assert svc.has_used_message("recent")

    # The file is rewritten compacted, so stale ids don't accumulate forever.
    with open(log_path) as f:
        ids = {json.loads(line)["id"] for line in f if line.strip()}
    assert ids == {"recent"}


@pytest.mark.asyncio
async def test_process_response_skips_replayed_message_id(config, storage_path):
    """A replayed message id is routed to the service exactly once."""
    svc = NtfyService(config, storage_path)
    svc.process_response = AsyncMock()
    api = NtfyAPI(config, service_callback=svc)

    tap = {
        "id": "tap-1",
        "event": "message",
        "time": 1749370000,
        "message": "Yes, game started at 10:00",
    }

    # First delivery: consumed and dispatched to the service.
    await api._process_response(dict(tap))
    # ?since=24h reconnect redelivers the SAME id -> must be dropped.
    await api._process_response(dict(tap))
    # Let the create_task'd dispatch from the first delivery run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert svc.process_response.call_count == 1
    assert svc.has_used_message("tap-1")


@pytest.mark.asyncio
async def test_distinct_message_ids_both_processed(config, storage_path):
    """Two genuinely different taps (distinct ids) are both processed."""
    svc = NtfyService(config, storage_path)
    svc.process_response = AsyncMock()
    api = NtfyAPI(config, service_callback=svc)

    await api._process_response(
        {"id": "a", "event": "message", "time": 1, "message": "No, not yet at 00:00"}
    )
    await api._process_response(
        {"id": "b", "event": "message", "time": 2, "message": "No, not yet at 05:00"}
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert svc.process_response.call_count == 2
