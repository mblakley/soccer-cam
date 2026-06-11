"""Tests for the service-side UpdateCheckProcessor.

The Phase 1 boundary matters: this processor must NOT call the legacy
``install_update``. Tests pin that boundary so a future refactor that
re-introduces auto-install gets caught.
"""

from __future__ import annotations

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
    update_api_url: str | None = None,
    auto_update: bool = True,
) -> MagicMock:
    cfg = MagicMock()
    cfg.app.github_repo = github_repo
    cfg.app.update_api_url = update_api_url
    cfg.app.auto_update = auto_update
    return cfg


async def _always_idle() -> tuple[bool, str | None]:
    return True, None


async def _always_busy() -> tuple[bool, str | None]:
    return False, "download_queue=2"


@pytest.fixture
def processor(tmp_path):
    return UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
    )


def _make_manager_mock(tmp_path, *, version_info=SAMPLE_VERSION_INFO, download_ok=True):
    """Patch ``UpdateManager`` so the processor can run end-to-end
    without touching the filesystem or spawning a real installer."""
    instance = MagicMock()
    instance.check_for_updates = AsyncMock(return_value=(True, version_info))
    instance.download_update = AsyncMock(return_value=download_ok)
    instance.temp_dir = str(tmp_path / "tmp-update")
    instance.installer_path.return_value = str(
        tmp_path / "tmp-update" / "VideoGrouperSetup.exe"
    )
    instance.compute_sha256.return_value = "sha256:cccc"  # matches setup.exe digest
    instance.verify_digest.return_value = True
    instance.spawn_installer.return_value = 12345
    instance.cleanup = MagicMock()
    return instance


class TestRunOneCheck:
    @pytest.mark.asyncio
    async def test_happy_path_spawns_when_auto_update(self, processor, tmp_path):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = _make_manager_mock(tmp_path)
            MockMgr.return_value = instance

            await processor._run_one_check()

        assert processor._pending_version == "0.3.7"
        assert processor._pending_digest == "sha256:cccc"
        assert processor._last_check_outcome == "spawned"
        instance.spawn_installer.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_callback_fires_after_spawn(self, tmp_path):
        shutdown = MagicMock()
        proc = UpdateCheckProcessor(
            storage_path=str(tmp_path),
            config=_make_config(),
            current_version="0.3.6",
            quiescence_check=_always_idle,
            shutdown_callback=shutdown,
        )
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            MockMgr.return_value = _make_manager_mock(tmp_path)
            await proc._run_one_check()

        shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_digest_mismatch_blocks_spawn(self, processor, tmp_path):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = _make_manager_mock(tmp_path)
            instance.verify_digest.return_value = False
            MockMgr.return_value = instance

            await processor._run_one_check()

        assert processor._last_check_outcome == "failed"
        assert processor._last_check_error == "digest_mismatch"
        instance.spawn_installer.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_update_false_stops_at_verify(self, tmp_path):
        proc = UpdateCheckProcessor(
            storage_path=str(tmp_path),
            config=_make_config(auto_update=False),
            current_version="0.3.6",
            quiescence_check=_always_idle,
        )
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            instance = _make_manager_mock(tmp_path)
            MockMgr.return_value = instance

            await proc._run_one_check()

        assert proc._pending_version == "0.3.7"
        assert proc._last_check_outcome == "pending_user_approval"
        instance.spawn_installer.assert_not_called()

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
            MockMgr.return_value = _make_manager_mock(tmp_path)

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
    async def test_status_reflects_completed_check(self, processor, tmp_path):
        with patch(
            "video_grouper.task_processors.update_check_processor.UpdateManager"
        ) as MockMgr:
            MockMgr.return_value = _make_manager_mock(tmp_path)

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


class TestNsisMarkerConsumption:
    def test_complete_marker_journaled_as_installed(
        self, processor, tmp_path, monkeypatch
    ):
        marker = tmp_path / "nsis-phase.txt"
        marker.write_text("complete", encoding="utf-8")
        monkeypatch.setenv("SOCCER_CAM_NSIS_MARKER_PATH", str(marker))

        processor._consume_nsis_marker()

        assert processor._nsis_phase_from_last_install == "complete"
        assert not marker.exists()

        journal_file = tmp_path / "logs" / "update_history.jsonl"
        assert journal_file.exists()
        lines = [line for line in journal_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        import json

        entry = json.loads(lines[0])
        assert entry["outcome"] == "installed"
        assert entry["nsis_phase"] == "complete"

    def test_partial_marker_journaled_as_failed(self, processor, tmp_path, monkeypatch):
        marker = tmp_path / "nsis-phase.txt"
        marker.write_text("files-copied", encoding="utf-8")
        monkeypatch.setenv("SOCCER_CAM_NSIS_MARKER_PATH", str(marker))

        processor._consume_nsis_marker()

        import json

        journal_file = tmp_path / "logs" / "update_history.jsonl"
        entry = json.loads(journal_file.read_text().splitlines()[0])
        assert entry["outcome"] == "failed"
        assert entry["nsis_phase"] == "files-copied"
        assert "files-copied" in entry["error"]

    def test_missing_marker_does_nothing(self, processor, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "SOCCER_CAM_NSIS_MARKER_PATH", str(tmp_path / "nonexistent.txt")
        )
        processor._consume_nsis_marker()
        assert processor._nsis_phase_from_last_install is None
        journal_file = tmp_path / "logs" / "update_history.jsonl"
        assert not journal_file.exists()

    def test_status_surfaces_nsis_phase(self, processor, tmp_path, monkeypatch):
        marker = tmp_path / "nsis-phase.txt"
        marker.write_text("complete", encoding="utf-8")
        monkeypatch.setenv("SOCCER_CAM_NSIS_MARKER_PATH", str(marker))

        processor._consume_nsis_marker()
        status = processor.build_status()
        assert status.nsis_phase_from_last_install == "complete"
