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


def test_job_capabilities_without_a_camera_omit_the_camera():
    """Setup's camera step can be skipped; an install with no camera still
    starts and announces what it can do."""
    from video_grouper.utils.config import Config, StorageConfig

    config = Config(storage=StorageConfig(path="/data"))
    assert config.cameras == []
    caps = job_capabilities(config)
    assert caps["ffmpeg"] is True
    assert "camera_type" not in caps and "camera_ip" not in caps


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
async def test_a_raising_heartbeat_is_logged_and_the_next_tick_runs(tmp_path, caplog):
    link = FakeDeviceLink(errors={"heartbeat": RuntimeError("bug")})
    processor = _processor(tmp_path, link)
    with caplog.at_level(logging.ERROR):
        await processor.discover_work()  # does not raise
        await processor.discover_work()
    assert link.heartbeats == 2
    assert "heartbeat raised" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb",
    ["register", "register_cameras", "stitch_profiles", "report_stuck_downloads"],
)
async def test_one_raising_step_does_not_skip_the_heartbeat_or_commands(
    tmp_path, verb, caplog
):
    link = FakeDeviceLink.replying(
        commands=[DeviceCommand("k-1", "restart")], errors={verb: RuntimeError("bug")}
    )
    link.camera_ids = {"front": "c-1"}
    processor = _processor(
        tmp_path,
        link,
        capabilities={"ffmpeg": True},
        cameras=lambda: [_Camera("front")],
        command_executor=_executor(),
    )
    with caplog.at_level(logging.ERROR):
        await processor.discover_work()
    assert link.heartbeats == 1
    assert [c[:2] for c in link.command_calls] == [("ack", "k-1"), ("complete", "k-1")]
    assert "raised; the other steps still run" in caplog.text


@pytest.mark.asyncio
async def test_a_raising_command_does_not_skip_the_next(tmp_path):
    link = FakeDeviceLink.replying(
        commands=[DeviceCommand("k-1", "restart"), DeviceCommand("k-2", "restart")]
    )
    executor = MagicMock()
    executor.execute = AsyncMock(
        side_effect=[RuntimeError("bug"), {"success": True, "message": "ok"}]
    )
    processor = _processor(tmp_path, link, command_executor=executor)
    await processor.discover_work()
    assert ("complete", "k-2", {"success": True, "message": "ok"}) in link.command_calls


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
async def test_failed_camera_registration_backs_off(tmp_path, monkeypatch):
    """A failed attempt (no ids back: link refused, signed out, 404 ...) is
    retried after 5 min, doubling up to the hourly interval -- not every tick."""
    import video_grouper.task_processors.device_link_processor as dlp

    clock = [1000.0]
    monkeypatch.setattr(dlp.time, "monotonic", lambda: clock[0])
    link = FakeDeviceLink()  # returns no ids
    processor = _processor(tmp_path, link, cameras=lambda: [_Camera("front")])

    attempts = []
    for _ in range(2 * 60 * 2):  # two hours of 30 s ticks
        await processor.discover_work()
        attempts.append(len(link.registered_cameras))
        clock[0] += 30
    # tries at 0, 5, 15, 35 and 75 min (waits of 5, 10, 20, 40, then 60 min)
    assert len(link.registered_cameras) == 5
    assert attempts[:10] == [1] * 10  # nothing more in the first 5 minutes
    assert attempts[10] == 2

    link.camera_ids = {"front": "c-1"}
    clock[0] += 3600
    await processor.discover_work()
    assert len(link.registered_cameras) == 6
    clock[0] += 3599
    await processor.discover_work()
    assert len(link.registered_cameras) == 6  # success: hourly from here


@pytest.mark.asyncio
async def test_offline_cameras_are_not_asked_every_tick(tmp_path):
    asked = []

    class _Offline(_Camera):
        async def get_device_info(self):
            asked.append(1)
            raise OSError("offline")

    link = FakeDeviceLink()
    processor = _processor(tmp_path, link, cameras=lambda: [_Offline("front")])
    for _ in range(10):
        await processor.discover_work()
    assert len(asked) == 1
    assert link.registered_cameras == []


@pytest.mark.asyncio
async def test_cameras_are_not_described_when_the_link_cannot_register(tmp_path):
    from video_grouper.integrations.device_link.sources.null import NullDeviceLink

    asked = []

    class _Counting(_Camera):
        async def get_device_info(self):
            asked.append(1)
            return await super().get_device_info()

    for link in (FakeDeviceLink(registers_cameras=False), NullDeviceLink()):
        processor = _processor(tmp_path, link, cameras=lambda: [_Counting("front")])
        await processor.discover_work()
        await processor.discover_work()
    assert asked == []


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
async def test_a_pulled_profile_lands_where_the_local_calibration_writes(tmp_path):
    """Always <storage>/stitch_profile.json -- the file the calibration page
    writes -- so newer-wins compares the two calibrations, whatever
    seam_realign_profile_path says."""
    import json

    from video_grouper.utils.stitch_remap import local_profile_path

    link = FakeDeviceLink()
    link.profiles = {"front": _PROFILE}
    processor = _processor(tmp_path, link)
    processor.config.processing.seam_realign_profile_path = str(tmp_path / "x.json")
    await processor.discover_work()
    assert local_profile_path(tmp_path) == tmp_path / "stitch_profile.json"
    assert json.loads((tmp_path / "stitch_profile.json").read_text()) == _PROFILE
    assert not (tmp_path / "x.json").exists()


@pytest.mark.asyncio
async def test_different_profiles_for_two_cameras_install_neither(tmp_path):
    link = FakeDeviceLink()
    other = dict(_PROFILE, seam_x=3000)
    link.profiles = {"front": _PROFILE, "back": other}
    processor = _processor(tmp_path, link)
    processor.config.processing.seam_realign_profile_path = None
    await processor.discover_work()
    assert not (tmp_path / "stitch_profile.json").exists()


# ---------------------------------------------------------------------------
# Stuck downloads: reported when they change, hourly regardless
# ---------------------------------------------------------------------------

_GROUP = "2026.07.01-10.00.00"


def _stuck_group(storage, files, *, name=_GROUP):
    """A group dir whose state.json holds ``files``: basename -> entry."""
    import json
    from pathlib import Path

    group_dir = Path(storage) / name
    group_dir.mkdir(parents=True, exist_ok=True)  # Path.mkdir: os.makedirs is patched
    (group_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "files": {str(group_dir / n): e for n, e in files.items()},
            }
        ),
        encoding="utf-8",
    )


def _failing(camera="front", attempts=2, error="timed out"):
    return {
        "status": "download_failed",
        "start_time": "2026-07-01T10:00:00",
        "end_time": "2026-07-01T10:30:00",
        "failed_attempts": attempts,
        "error_message": error,
        "last_failed_at": "2026-07-01T12:00:00",
        "metadata": {"camera_name": camera},
    }


@pytest.fixture
def clock(monkeypatch):
    import video_grouper.task_processors.device_link_processor as dlp

    now = [1000.0]
    monkeypatch.setattr(dlp.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_first_tick_reports_the_stuck_downloads_in_soccer_cam_terms(tmp_path):
    from video_grouper.integrations.device_link import StuckDownload

    _stuck_group(tmp_path, {"a.mp4": _failing(), "ok.mp4": {"status": "downloaded"}})
    link = FakeDeviceLink()
    await _processor(tmp_path, link).discover_work()
    assert link.stuck_reports == [
        [
            StuckDownload(
                camera="front",
                group=_GROUP,
                file_name="a.mp4",
                recorded_at="2026-07-01T10:00:00",
                failed_attempts=2,
                error="timed out",
                last_failed_at="2026-07-01T12:00:00",
            )
        ]
    ]


@pytest.mark.asyncio
async def test_an_empty_list_is_reported_on_the_first_tick(tmp_path):
    """Nothing stuck still goes out once: it clears a stale list elsewhere."""
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    await processor.discover_work()
    assert link.stuck_reports == [[]]


@pytest.mark.asyncio
async def test_an_unchanged_list_is_not_re_sent_within_the_hour(tmp_path, clock):
    _stuck_group(tmp_path, {"a.mp4": _failing()})
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    for _ in range(119):  # just under an hour of 30 s ticks
        await processor.discover_work()
        clock[0] += 30
    assert len(link.stuck_reports) == 1


@pytest.mark.asyncio
async def test_a_changed_list_is_sent_at_once(tmp_path, clock):
    _stuck_group(tmp_path, {"a.mp4": _failing(attempts=2)})
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    clock[0] += 30
    _stuck_group(tmp_path, {"a.mp4": _failing(attempts=3)})  # another round failed
    await processor.discover_work()
    clock[0] += 30
    _stuck_group(tmp_path, {"a.mp4": _failing(attempts=3, camera=None)})
    _stuck_group(tmp_path, {"b.mp4": _failing()}, name="2026.07.02-10.00.00")
    await processor.discover_work()
    assert [
        [(s.file_name, s.failed_attempts) for s in r] for r in link.stuck_reports
    ] == [
        [("a.mp4", 2)],
        [("a.mp4", 3)],
        [("a.mp4", 3), ("b.mp4", 2)],
    ]
    assert link.stuck_reports[-1][0].camera is None


@pytest.mark.asyncio
async def test_an_unchanged_list_is_re_sent_hourly(tmp_path, clock):
    """So the other end can tell the report is fresh."""
    _stuck_group(tmp_path, {"a.mp4": _failing()})
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    clock[0] += 3599
    await processor.discover_work()
    assert len(link.stuck_reports) == 1
    clock[0] += 1
    await processor.discover_work()
    assert len(link.stuck_reports) == 2
    assert link.stuck_reports[0] == link.stuck_reports[1]


@pytest.mark.asyncio
async def test_a_report_the_link_could_not_deliver_is_tried_next_tick(tmp_path):
    link = FakeDeviceLink()
    link.accepts_stuck_reports = False
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    await processor.discover_work()
    link.accepts_stuck_reports = True
    await processor.discover_work()
    await processor.discover_work()
    assert len(link.stuck_reports) == 3  # two refused, then delivered once


@pytest.mark.asyncio
async def test_new_camera_ids_resend_the_list(tmp_path, clock):
    """A stuck file whose camera had no id yet went out without it; once the
    camera registers, the same list is worth sending again."""
    _stuck_group(tmp_path, {"a.mp4": _failing()})
    link = FakeDeviceLink()  # registration gets no ids back
    processor = _processor(tmp_path, link, cameras=lambda: [_Camera("front")])
    await processor.discover_work()
    assert len(link.stuck_reports) == 1
    link.camera_ids = {"front": "c-1"}
    clock[0] += 300  # the registration retry
    await processor.discover_work()
    assert len(link.stuck_reports) == 2
    clock[0] += 3600  # hourly re-registration, same ids: just the hourly resend
    await processor.discover_work()
    assert len(link.stuck_reports) == 3


@pytest.mark.asyncio
async def test_nothing_is_reported_when_the_link_has_no_account(tmp_path):
    from video_grouper.integrations.device_link.sources.null import NullDeviceLink

    _stuck_group(tmp_path, {"a.mp4": _failing()})
    off = FakeDeviceLink(registers_cameras=False)
    await _processor(tmp_path, off).discover_work()
    assert off.stuck_reports == []

    null = NullDeviceLink()
    null.report_stuck_downloads = AsyncMock()  # type: ignore[method-assign]
    await _processor(tmp_path, null).discover_work()
    null.report_stuck_downloads.assert_not_called()


@pytest.mark.asyncio
async def test_signing_back_in_reports_again_at_once(tmp_path, clock):
    _stuck_group(tmp_path, {"a.mp4": _failing()})
    link = FakeDeviceLink()
    processor = _processor(tmp_path, link)
    await processor.discover_work()
    link.can_register_cameras = False  # signed out
    clock[0] += 30
    await processor.discover_work()
    link.can_register_cameras = True
    clock[0] += 30
    await processor.discover_work()
    assert len(link.stuck_reports) == 2


@pytest.mark.asyncio
async def test_a_raising_stuck_list_does_not_stop_the_heartbeat(
    tmp_path, monkeypatch, caplog
):
    import video_grouper.task_processors.device_link_processor as dlp

    def boom(storage_path):
        raise OSError("storage gone")

    monkeypatch.setattr(dlp, "list_stuck_downloads", boom)
    link = FakeDeviceLink.replying(commands=[DeviceCommand("k-1", "restart")])
    processor = _processor(tmp_path, link, command_executor=_executor())
    with caplog.at_level(logging.ERROR):
        await processor.discover_work()
    assert link.heartbeats == 1
    assert [c[:2] for c in link.command_calls] == [("ack", "k-1"), ("complete", "k-1")]
    assert link.stuck_reports == []
    assert "stuck downloads report raised" in caplog.text


@pytest.mark.asyncio
async def test_stuck_downloads_are_reported_even_when_the_heartbeat_raises(tmp_path):
    link = FakeDeviceLink(errors={"heartbeat": RuntimeError("bug")})
    await _processor(tmp_path, link).discover_work()
    assert link.stuck_reports == [[]]
