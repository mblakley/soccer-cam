"""DeviceLinkProcessor against the interface (FakeDeviceLink): announce when it
takes jobs, heartbeat every tick, keep the camera-enabled state, and run each
command acknowledge -> execute -> complete. The TTT mapping is tested in
tests/integrations/device_link/test_ttt_device_link.py."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from video_grouper.integrations.device_link import DeviceCommand, HeartbeatReply
from video_grouper.integrations.device_link.fake import FakeDeviceLink
from video_grouper.task_processors.device_link_processor import (
    DeviceLinkProcessor,
    job_capabilities,
)


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _processor(tmp_path, link, **kwargs):
    return DeviceLinkProcessor(str(tmp_path), MagicMock(), link, **kwargs)


def _executor(result=None):
    executor = MagicMock()
    executor.execute = AsyncMock(
        return_value=result or {"success": True, "message": "ok"}
    )
    return executor


def test_ticks_on_the_links_cadence(tmp_path):
    assert _processor(tmp_path, FakeDeviceLink(heartbeat_interval=7)).poll_interval == 7


@pytest.mark.asyncio
async def test_heartbeats_every_tick(tmp_path):
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    await processor.discover_work()
    assert link.heartbeats == 2


@pytest.mark.asyncio
async def test_announces_capabilities_every_tick_when_taking_jobs(tmp_path):
    # The link makes register idempotent; the processor just keeps asking.
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link, capabilities={"ffmpeg": True})
    await processor.discover_work()
    await processor.discover_work()
    assert link.registered == [{"ffmpeg": True}, {"ffmpeg": True}]


@pytest.mark.asyncio
async def test_does_not_announce_when_not_taking_jobs(tmp_path):
    link = FakeDeviceLink()
    await _processor(tmp_path, link).discover_work()
    assert link.registered == []
    assert link.heartbeats == 1


def test_job_capabilities_come_from_local_config():
    config = MagicMock()
    config.pipeline.is_active.return_value = True
    config.camera.type = "reolink"
    config.camera.device_ip = "10.0.0.5"
    assert job_capabilities(config) == {
        "ffmpeg": True,
        "pipeline": True,
        "camera_type": "reolink",
        "camera_ip": "10.0.0.5",
    }


# ---------------------------------------------------------------------------
# Camera enabled state
# ---------------------------------------------------------------------------


def test_every_camera_enabled_before_any_heartbeat(tmp_path):
    assert _processor(tmp_path, FakeDeviceLink()).is_camera_enabled("cam") is True


@pytest.mark.asyncio
async def test_reply_sets_camera_state_and_unlisted_cameras_stay_enabled(tmp_path):
    link = FakeDeviceLink.replying(camera_enabled={"front": False, "back": True})
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    assert processor.is_camera_enabled("front") is False
    assert processor.is_camera_enabled("back") is True
    assert processor.is_camera_enabled("unlisted") is True


@pytest.mark.asyncio
async def test_no_news_keeps_the_last_state_and_a_new_map_replaces_it(tmp_path):
    link = FakeDeviceLink(
        [
            HeartbeatReply(camera_enabled={"front": False}),
            HeartbeatReply(camera_enabled=None),  # e.g. the lookup failed
            HeartbeatReply(camera_enabled={}),  # nothing listed -> all enabled
        ]
    )
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    await processor.discover_work()
    assert processor.is_camera_enabled("front") is False
    await processor.discover_work()
    assert processor.is_camera_enabled("front") is True


@pytest.mark.asyncio
async def test_logs_when_a_camera_is_disabled_here(tmp_path, caplog):
    link = FakeDeviceLink(
        [
            HeartbeatReply(camera_enabled={"front": True}),
            HeartbeatReply(camera_enabled={"front": False}),
        ]
    )
    processor = _processor(tmp_path, link)
    with caplog.at_level(logging.INFO):
        await processor.discover_work()
        assert "disabled on this machine" not in caplog.text
        await processor.discover_work()
    assert "camera front disabled on this machine" in caplog.text


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_command_is_acknowledged_executed_then_completed(tmp_path):
    first = DeviceCommand("c1", "restart")
    second = DeviceCommand("c2", "delete_old_files", {"older_than_days": 7})
    link = FakeDeviceLink.replying(commands=[first, second])
    order: list[tuple] = []

    async def execute(command):
        order.append(("execute", command.id))
        return {"success": True, "message": command.type}

    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=execute)

    async def ack(command_id):
        order.append(("ack", command_id))

    async def complete(command_id, result):
        order.append(("complete", command_id, result))

    link.acknowledge_command = ack  # type: ignore[method-assign]
    link.complete_command = complete  # type: ignore[method-assign]

    await _processor(tmp_path, link, command_executor=executor).discover_work()

    assert order == [
        ("ack", "c1"),
        ("execute", "c1"),
        ("complete", "c1", {"success": True, "message": "restart"}),
        ("ack", "c2"),
        ("execute", "c2"),
        ("complete", "c2", {"success": True, "message": "delete_old_files"}),
    ]
    assert executor.execute.await_args_list[1].args[0] is second


@pytest.mark.asyncio
async def test_without_an_executor_a_command_is_completed_as_failed(tmp_path):
    link = FakeDeviceLink.replying(commands=[DeviceCommand("c1", "restart")])
    await _processor(tmp_path, link).discover_work()
    assert link.command_calls == [
        ("ack", "c1"),
        (
            "complete",
            "c1",
            {"success": False, "message": "Command executor not available"},
        ),
    ]


@pytest.mark.asyncio
async def test_executor_result_is_reported(tmp_path):
    link = FakeDeviceLink.replying(commands=[DeviceCommand("c1", "eject")])
    executor = _executor({"success": False, "message": "Unknown command: eject"})
    await _processor(tmp_path, link, command_executor=executor).discover_work()
    assert link.command_calls[-1] == (
        "complete",
        "c1",
        {"success": False, "message": "Unknown command: eject"},
    )


@pytest.mark.asyncio
async def test_no_commands_no_command_calls(tmp_path):
    link = FakeDeviceLink.replying(camera_enabled={"front": True})
    executor = _executor()
    await _processor(tmp_path, link, command_executor=executor).discover_work()
    assert link.command_calls == []
    executor.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Loop: a link that breaks its no-raise promise doesn't stop the heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_a_raising_link(tmp_path):
    import asyncio

    link = FakeDeviceLink(heartbeat_interval=0, errors={"heartbeat": RuntimeError()})
    processor = _processor(tmp_path, link)
    await processor.start()
    for _ in range(50):
        if link.heartbeats >= 2:
            break
        await asyncio.sleep(0)
    await processor.stop()
    assert link.heartbeats >= 2


@pytest.mark.asyncio
async def test_each_heartbeat_carries_this_installs_health(tmp_path):
    from unittest.mock import patch

    from video_grouper.version import __version__

    tracker = MagicMock()
    tracker.get_last_error.return_value = "disk full"
    tracker.get_error_count_24h.return_value = 3
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link, error_tracker=tracker)
    with patch(
        "video_grouper.utils.system_metrics.get_system_metrics",
        return_value={"cpu_usage_percent": 7.0, "disk_free_gb": 120.5},
    ):
        await processor.discover_work()
    (health,) = link.health_reports
    assert health.cpu_usage_percent == 7.0 and health.disk_free_gb == 120.5
    assert health.version == __version__
    assert (health.last_error, health.error_count_24h) == ("disk full", 3)


class _Camera:
    def __init__(self, name, serial="SER", fail=False):
        self.name = name
        self.config = MagicMock(type="reolink")
        self._serial, self._fail = serial, fail

    async def get_device_info(self):
        if self._fail:
            raise OSError("offline")
        return {
            "serial_number": self._serial,
            "model": "RLC-81MA",
            "mac_address": "aa:bb",
        }


@pytest.mark.asyncio
async def test_registers_cameras_that_can_identify_themselves(tmp_path):
    from video_grouper.integrations.device_link import DeviceCamera

    link = FakeDeviceLink()
    link.camera_ids = {"front": "c-1"}
    cams = [_Camera("front", "SER-1"), _Camera("dark", fail=True), _Camera("x", "")]
    processor = _processor(tmp_path, link, cameras=lambda: cams)
    await processor.discover_work()
    assert link.registered_cameras == [
        [DeviceCamera("front", "SER-1", "reolink", "RLC-81MA", "aa:bb")]
    ]
    await processor.discover_work()  # registered: not again within the hour
    assert len(link.registered_cameras) == 1


@pytest.mark.asyncio
async def test_camera_registration_retries_until_it_succeeds(tmp_path):
    link = FakeDeviceLink()  # returns no ids: not registered yet (signed out, ...)
    processor = _processor(tmp_path, link, cameras=lambda: [_Camera("front")])
    await processor.discover_work()
    await processor.discover_work()
    assert len(link.registered_cameras) == 2
    link.camera_ids = {"front": "c-1"}
    await processor.discover_work()
    await processor.discover_work()
    assert len(link.registered_cameras) == 3


@pytest.mark.asyncio
async def test_cameras_are_re_announced_hourly(tmp_path, monkeypatch):
    import video_grouper.task_processors.device_link_processor as dlp

    clock = [1000.0]
    monkeypatch.setattr(dlp.time, "monotonic", lambda: clock[0])
    link = FakeDeviceLink()
    link.camera_ids = {"front": "c-1"}
    processor = _processor(tmp_path, link, cameras=lambda: [_Camera("front")])
    await processor.discover_work()
    clock[0] += 3599
    await processor.discover_work()
    clock[0] += 2
    await processor.discover_work()
    assert len(link.registered_cameras) == 2


_PROFILE = {
    "source_width": 7680,
    "source_height": 2160,
    "seam_x": 3840,
    "dx_anchors": [[0, -10], [2160, 0]],
    "metadata": {"created_utc": "2026-11-27T10:00:00+00:00"},
}


@pytest.mark.asyncio
async def test_a_profile_from_the_link_is_installed_where_the_pipeline_reads(tmp_path):
    import json

    link = FakeDeviceLink()
    link.profiles = {"front": _PROFILE}
    processor = _processor(tmp_path, link)
    processor.config.processing.seam_realign_profile_path = None
    await processor.discover_work()
    assert json.loads((tmp_path / "stitch_profile.json").read_text()) == _PROFILE


@pytest.mark.asyncio
async def test_different_profiles_for_two_cameras_install_neither(tmp_path):
    link = FakeDeviceLink()
    other = dict(_PROFILE, seam_x=3000)
    link.profiles = {"front": _PROFILE, "back": other}
    processor = _processor(tmp_path, link)
    processor.config.processing.seam_realign_profile_path = None
    await processor.discover_work()
    assert not (tmp_path / "stitch_profile.json").exists()
