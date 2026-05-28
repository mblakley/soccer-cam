"""Tests for the service-side UpdateCheckProcessor.

The Phase 1 boundary matters: this processor must NOT call the legacy
``install_update``. Tests pin that boundary so a future refactor that
re-introduces auto-install gets caught.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_grouper.task_processors.update_check_processor import (
    UpdateCheckProcessor,
    UpdateStatus,
)
from video_grouper.update.update_manager import NetworkError


SAMPLE_VERSION_INFO = {
    "version": "0.3.7",
    "tag_name": "v0.3.7",
    "release_date": "2026-05-27T00:00:00Z",
    "release_notes": "release notes",
    "assets": [
        {
            "name": "VideoGrouperService.exe",
            "browser_download_url": "https://x/VideoGrouperService.exe",
            "size": 50_000_000,
            "digest": "sha256:aaaa",
        },
        {
            "name": "VideoGrouperTray.exe",
            "browser_download_url": "https://x/VideoGrouperTray.exe",
            "size": 40_000_000,
            "digest": "sha256:bbbb",
        },
        {
            "name": "VideoGrouperSetup.exe",
            "browser_download_url": "https://x/VideoGrouperSetup.exe",
            "size": 90_000_000,
            "digest": "sha256:cccc",
        },
    ],
}


def _make_config(
    *,
    github_repo: str = "mblakley/soccer-cam",
    update_api_url: Optional[str] = None,
    auto_update: bool = True,
) -> MagicMock:
    cfg = MagicMock()
    cfg.app.github_repo = github_repo
    cfg.app.update_api_url = update_api_url
    cfg.app.auto_update = auto_update
    return cfg


async def _always_idle() -> tuple[bool, Optional[str]]:
    return True, None


async def _always_busy() -> tuple[bool, Optional[str]]:
    return False, "download_queue=2"


@pytest.fixture
def processor(tmp_path):
    return UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
    )


class TestRunOneCheck:
    @pytest.mark.asyncio
    async def test_happy_path_stages_pending(self, processor, tmp_path):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(
                return_value=(True, SAMPLE_VERSION_INFO)
            )
            instance.download_update = AsyncMock(return_value=True)
            instance.temp_dir = str(tmp_path / "tmp-update")

            await processor._run_one_check()

        assert processor._pending_version == "0.3.7"
        assert processor._pending_digest == "sha256:cccc"  # prefers setup.exe digest
        assert processor._last_check_outcome == "spawned"
        # Phase 1 boundary: install_update is NOT called.
        instance.install_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_update_clears_outcome(self, processor):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(return_value=(False, None))

            await processor._run_one_check()

        assert processor._last_check_outcome == "skipped"
        assert processor._pending_version is None

    @pytest.mark.asyncio
    async def test_quiescence_defer_records_reason(self, tmp_path):
        proc = UpdateCheckProcessor(
            storage_path=str(tmp_path),
            config=_make_config(),
            current_version="0.3.6",
            quiescence_check=_always_busy,
        )
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(
                return_value=(True, SAMPLE_VERSION_INFO)
            )
            instance.download_update = AsyncMock()

            await proc._run_one_check()

        assert proc._last_check_outcome == "deferred"
        assert proc._last_check_deferred_reason == "download_queue=2"
        # Pending version is still surfaced so the dashboard can show
        # "0.3.7 detected, deferred until idle".
        assert proc._pending_version == "0.3.7"
        instance.download_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_error_recorded(self, processor):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(side_effect=NetworkError("offline"))

            await processor._run_one_check()

        assert processor._last_check_outcome == "failed"
        assert processor._last_check_error is not None
        assert "offline" in processor._last_check_error

    @pytest.mark.asyncio
    async def test_download_failure_recorded(self, processor):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(
                return_value=(True, SAMPLE_VERSION_INFO)
            )
            instance.download_update = AsyncMock(return_value=False)

            await processor._run_one_check()

        assert processor._last_check_outcome == "failed"
        assert processor._last_check_error == "download_failed"

    @pytest.mark.asyncio
    async def test_journal_entry_appended(self, processor, tmp_path):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(
                return_value=(True, SAMPLE_VERSION_INFO)
            )
            instance.download_update = AsyncMock(return_value=True)

            await processor._run_one_check()

        journal_file = tmp_path / "logs" / "update_history.jsonl"
        assert journal_file.exists()
        lines = journal_file.read_text().splitlines()
        assert len(lines) == 1


class TestBuildStatus:
    def test_status_initial_state(self, processor):
        status = processor.build_status()
        assert isinstance(status, UpdateStatus)
        assert status.current_version == "0.3.6"
        assert status.auto_update is True
        assert status.pending_version is None
        assert status.currently_checking is False
        assert status.last_check_at is None

    @pytest.mark.asyncio
    async def test_status_reflects_completed_check(self, processor):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = MockMgr.return_value
            instance.check_for_updates = AsyncMock(
                return_value=(True, SAMPLE_VERSION_INFO)
            )
            instance.download_update = AsyncMock(return_value=True)

            await processor._run_one_check()

        status = processor.build_status()
        assert status.pending_version == "0.3.7"
        assert status.last_check_outcome == "spawned"
        assert status.last_check_at is not None

    def test_status_source_when_env_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOCCER_CAM_UPDATE_API_URL", "http://127.0.0.1:9876/r/l")
        proc = UpdateCheckProcessor(
            storage_path=str(tmp_path),
            config=_make_config(),
            current_version="0.3.6",
            quiescence_check=_always_idle,
        )
        status = proc.build_status()
        assert status.source == "env"
        assert status.source_url == "http://127.0.0.1:9876/r/l"


class TestImmediateCheckSignal:
    def test_request_immediate_check_is_idempotent(self, processor):
        processor.request_immediate_check()
        processor.request_immediate_check()
        # No raise, no state corruption. Event is set once.
        assert processor._immediate_check.is_set()
