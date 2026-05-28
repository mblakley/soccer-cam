"""Tests for the /api/update HTTP surface."""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_grouper.task_processors.update_check_processor import (
    UpdateCheckProcessor,
    UpdateStatus,
)
from video_grouper.web.update_api import build_router


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.app.github_repo = "mblakley/soccer-cam"
    cfg.app.update_api_url = None
    cfg.app.auto_update = True
    return cfg


async def _always_idle() -> tuple[bool, Optional[str]]:
    return True, None


@pytest.fixture
def processor(tmp_path):
    return UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
    )


@pytest.fixture
def client(processor):
    app = FastAPI()
    app.include_router(build_router(processor))
    return TestClient(app)


def test_status_returns_initial_snapshot(client):
    resp = client.get("/api/update/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_version"] == "0.3.6"
    assert body["pending_version"] is None
    assert body["auto_update"] is True
    assert body["history"] == []


def test_status_includes_history_after_check(client, processor):
    # Inject a fake completed check via the processor state.
    snapshot = UpdateStatus(
        current_version="0.3.6",
        auto_update=True,
        source_url="https://example.com",
        source="default",
        next_check_at=0.0,
        last_check_at=1.0,
        last_check_outcome="skipped",
        pending_version=None,
    )
    processor.build_status = lambda: snapshot

    # Also write a fake journal entry so /status surfaces history.
    from video_grouper.update.journal import (
        UpdateJournalEntry,
        append_entry,
    )

    entry = UpdateJournalEntry(
        id="abcd1234",
        started_at=1.0,
        from_version="0.3.6",
        source_url="https://example.com",
        auto_update=True,
    )
    entry.finalize("skipped")
    append_entry(processor.storage_path, entry)

    resp = client.get("/api/update/status")
    body = resp.json()
    assert body["last_check_outcome"] == "skipped"
    assert len(body["history"]) == 1
    assert body["history"][0]["id"] == "abcd1234"


def test_check_now_triggers_immediate_event(client, processor):
    assert not processor._immediate_check.is_set()
    resp = client.post("/api/update/check-now")
    assert resp.status_code == 202
    assert resp.json() == {"status": "scheduled"}
    assert processor._immediate_check.is_set()


def test_apply_returns_503_in_phase_1(client, processor):
    """Phase 1 boundary: /apply has no working install path. The
    endpoint exists so the tray can be built/tested against the real
    route shape, but it must signal 'unavailable' rather than 202 so
    callers don't think the apply succeeded."""
    processor._pending_version = "0.3.7"
    resp = client.post("/api/update/apply")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["pending_version"] == "0.3.7"
    assert "Phase 2" in body["reason"]
