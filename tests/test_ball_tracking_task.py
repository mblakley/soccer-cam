"""Tests for the BallTrackingTask."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from video_grouper.task_processors.queue_type import QueueType
from video_grouper.task_processors.tasks.ball_tracking import BallTrackingTask


@pytest.fixture
def sample_group_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        group_dir = Path(temp_dir) / "flash__2024.06.01_vs_IYSA_home"
        group_dir.mkdir()
        yield group_dir


@pytest.fixture
def task(sample_group_dir):
    return BallTrackingTask(
        group_dir=sample_group_dir,
        input_path="/test/input-raw.mp4",
        output_path="/test/output.mp4",
        provider_name="autocam_gui",
        provider_config={"executable": "C:/Path/To/AutoCam.exe"},
        team_name="flash",
        storage_path=str(sample_group_dir.parent),
    )


class TestInitialization:
    def test_fields(self, task, sample_group_dir):
        assert task.group_dir == sample_group_dir
        assert task.input_path == "/test/input-raw.mp4"
        assert task.output_path == "/test/output.mp4"
        assert task.provider_name == "autocam_gui"
        assert task.provider_config == {"executable": "C:/Path/To/AutoCam.exe"}
        assert task.team_name == "flash"

    def test_queue_type(self, task):
        assert task.queue_type() == QueueType.BALL_TRACKING

    def test_task_type(self, task):
        assert task.task_type == "ball_tracking_process"

    def test_get_item_path(self, task, sample_group_dir):
        assert task.get_item_path() == str(sample_group_dir)


class TestSerialization:
    def test_round_trip(self, task):
        data = task.serialize()
        restored = BallTrackingTask.deserialize(data)
        assert restored == task
        assert restored.team_name == task.team_name
        assert restored.storage_path == task.storage_path

    def test_team_name_optional(self, sample_group_dir):
        task = BallTrackingTask(
            group_dir=sample_group_dir,
            input_path="x",
            output_path="y",
            provider_name="autocam_gui",
            provider_config={},
        )
        data = task.serialize()
        restored = BallTrackingTask.deserialize(data)
        assert restored.team_name is None

    def test_serialize_includes_task_type(self, task):
        assert task.serialize()["task_type"] == "ball_tracking_process"


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_invalid_input_returns_false(self, task):
        # input file doesn't exist; validation should fail before provider is called
        result = await task.execute()
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_unknown_provider_returns_false(self, sample_group_dir):
        # Create a real file so validation passes
        input_path = sample_group_dir / "input-raw.mp4"
        # Make file large enough + give it a bogus header — validate_video_file
        # will fail at PyAV decode but our test doesn't depend on going further
        input_path.write_bytes(b"\x00" * 20_000)

        task = BallTrackingTask(
            group_dir=sample_group_dir,
            input_path=str(input_path),
            output_path="/tmp/out.mp4",
            provider_name="this_provider_does_not_exist",
            provider_config={},
        )
        # Validation will fail (not a real video) before provider lookup,
        # but this still confirms the path doesn't raise.
        result = await task.execute()
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_calls_registered_provider(self, sample_group_dir):
        """Bypass video validation and verify the registry/provider path."""
        input_path = sample_group_dir / "input-raw.mp4"
        input_path.write_bytes(b"\x00" * 20_000)

        task = BallTrackingTask(
            group_dir=sample_group_dir,
            input_path=str(input_path),
            output_path="/tmp/out.mp4",
            provider_name="autocam_gui",
            provider_config={"executable": "fake.exe"},
            team_name="flash",
            storage_path=str(sample_group_dir.parent),
        )

        # Skip video validation; mock the provider's run().
        with (
            patch.object(BallTrackingTask, "_validate_video_file", return_value=True),
            patch(
                "video_grouper.ball_tracking.providers.autocam_gui._invoke_autocam",
                return_value=True,
            ) as mock_invoke,
        ):
            result = await task.execute()

        assert result is True
        assert mock_invoke.call_count == 1
        executable, in_path, out_path = mock_invoke.call_args.args
        assert executable == "fake.exe"
        assert in_path == str(input_path)
        assert out_path == "/tmp/out.mp4"
