"""Tests for NTFY topic-replay dedup via the consumed-message-id ledger.

The listener reconnects with ``?since=24h``, which redelivers every recent
topic message with its stable server-assigned id. Without a ledger of what we
already consumed, a replayed "Yes"/"No" tap can re-answer a freshly-queued
question on its own — the regression these tests pin down (2026-06 incident
where an earlier game's taps walked a new game's game-start question forward).

The NTFY topic is the source of truth; the ledger only records what we actually
used from it (record-on-use), so a dispatch that fails can still be retried by
the next replay.
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
    svc.record_used_message("abc", message="Yes, game started", decision="applied")
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


def test_record_truncates_long_message(config, storage_path):
    svc = NtfyService(config, storage_path)
    svc.record_used_message("long", message="x" * 500)
    with open(get_ntfy_processed_log_path(storage_path)) as f:
        entry = json.loads(f.readline())
    assert len(entry["message"]) == 200


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


def test_missing_or_malformed_recorded_at_is_kept(config, storage_path):
    """An entry whose recorded_at is absent/unparseable is kept (can't prove
    it's stale); a genuinely old one is still dropped. Also: a torn JSON line
    is skipped without losing the rest of the log."""
    log_path = get_ntfy_processed_log_path(storage_path)
    stale_at = (
        datetime.now() - timedelta(seconds=NTFY_PROCESSED_LOG_RETENTION_SECONDS + 3600)
    ).isoformat()
    with open(log_path, "w") as f:
        f.write(json.dumps({"id": "no-ts"}) + "\n")  # missing recorded_at
        f.write(json.dumps({"id": "bad-ts", "recorded_at": "not-a-date"}) + "\n")
        f.write("{torn json line\n")  # corrupt — must be skipped, not fatal
        f.write(json.dumps({"id": "stale", "recorded_at": stale_at}) + "\n")

    svc = NtfyService(config, storage_path)
    assert svc.has_used_message("no-ts")
    assert svc.has_used_message("bad-ts")
    assert not svc.has_used_message("stale")


@pytest.mark.asyncio
async def test_process_response_skips_replayed_message_id(config, storage_path):
    """A replayed message id is routed to the service exactly once (record-on-use)."""
    svc = NtfyService(config, storage_path)
    # Spy on the REAL process_response so it still records on consume.
    real = svc.process_response
    svc.process_response = AsyncMock(side_effect=real)
    api = NtfyAPI(config, service_callback=svc)

    tap = {
        "id": "tap-1",
        "event": "message",
        "time": 1749370000,
        "message": "Yes, game started at 10:00",
    }

    # First delivery: dispatched, consumed (buffered — no task waiting), recorded.
    await api._process_response(dict(tap))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # ?since=24h reconnect redelivers the SAME id -> skipped at ingest.
    await api._process_response(dict(tap))
    await asyncio.sleep(0)

    assert svc.process_response.call_count == 1
    assert svc.has_used_message("tap-1")
    # The replay never reached the buffer either (skipped before dispatch).
    assert len(svc._unmatched_responses) == 1


@pytest.mark.asyncio
async def test_failed_dispatch_is_not_recorded(config, storage_path):
    """If consuming the response fails, the id is NOT recorded — so the next
    ?since=24h replay can retry it. This is the whole point of record-on-use
    over record-on-ingest."""
    svc = NtfyService(config, storage_path)
    svc.process_response = AsyncMock(side_effect=RuntimeError("boom"))
    api = NtfyAPI(config, service_callback=svc)

    await api._process_response(
        {
            "id": "boom-1",
            "event": "message",
            "time": 1,
            "message": "Yes, game started at 00:00",
        }
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not svc.has_used_message("boom-1")


@pytest.mark.asyncio
async def test_echoed_notification_not_recorded(config, storage_path):
    """Our own echoed notification (carries title/actions/tags) is never
    consumed, so its id must not enter the ledger."""
    svc = NtfyService(config, storage_path)
    svc.process_response = AsyncMock()
    api = NtfyAPI(config, service_callback=svc)

    await api._process_response(
        {
            "id": "echo-1",
            "event": "message",
            "time": 1,
            "message": "Does the game start at 00:00?",
            "title": "Game Start Time",
            "actions": [{"action": "http", "label": "Yes"}],
        }
    )
    await asyncio.sleep(0)

    assert not svc.has_used_message("echo-1")
    assert svc.process_response.call_count == 0


@pytest.mark.asyncio
async def test_keepalive_event_not_recorded(config, storage_path):
    """Keepalive heartbeats carry an id but are never consumed."""
    svc = NtfyService(config, storage_path)
    svc.process_response = AsyncMock()
    api = NtfyAPI(config, service_callback=svc)

    await api._process_response({"id": "ka-1", "event": "keepalive", "time": 1})
    await asyncio.sleep(0)

    assert not svc.has_used_message("ka-1")
    assert svc.process_response.call_count == 0


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
