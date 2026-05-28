"""Tests for the /api/update HTTP surface."""

from __future__ import annotations

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


async def _always_idle() -> tuple[bool, str | None]:
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


def test_apply_without_pending_returns_409(client, processor):
    """No staged update -> 409. The endpoint refuses rather than
    silently no-op so a misbehaving tray surfaces the error."""
    assert processor._pending_version is None
    resp = client.post("/api/update/apply")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "rejected"
    assert "no update pending" in body["reason"].lower()


def test_apply_when_pipeline_busy_returns_503(client, processor):
    """Staged but busy -> 503 (retryable). User can wait for the
    queue to drain and click again, or rely on the polling loop's
    next quiescent tick."""
    processor._pending_version = "0.3.7"
    processor._pending_installer_path = "C:/fake/setup.exe"
    processor._pending_manager = MagicMock()

    async def busy():
        return False, "video_processor=1"

    processor.quiescence_check = busy

    resp = client.post("/api/update/apply")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "rejected"
    assert "video_processor" in body["reason"]


def test_apply_happy_path_spawns_and_returns_202(client, processor):
    """Pending + idle + apply -> processor spawns installer and
    returns 202. The fake UpdateManager records the spawn call."""
    fake_manager = MagicMock()
    fake_manager.spawn_installer.return_value = 1234
    processor._pending_version = "0.3.7"
    processor._pending_installer_path = "C:/fake/setup.exe"
    processor._pending_manager = fake_manager

    resp = client.post("/api/update/apply")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "spawned"
    assert body["pending_version"] == "0.3.7"
    fake_manager.spawn_installer.assert_called_once_with("C:/fake/setup.exe")
