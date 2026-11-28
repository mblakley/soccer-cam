"""TTTDeviceLink on its own: each verb maps onto the right TTT client calls
with the right arguments and gates (service calls only while signed in and
registered; camera status + commands only with a camera id; camera state only
with a machine id), and every call fails on its own without raising."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.api_integrations.ttt_api import TTTApiClient
from video_grouper.integrations.device_link import (
    DeviceCamera,
    DeviceCommand,
    HeartbeatReply,
)
from video_grouper.integrations.device_link.sources.ttt import TTTDeviceLink
from video_grouper.integrations.registry import IntegrationContext

CAPS = {"ffmpeg": True, "pipeline": False, "camera_type": "dahua", "camera_ip": "ip"}


def _client(signed_in=True):
    c = MagicMock(spec=TTTApiClient)
    c.ensure_signed_in.return_value = signed_in
    c.register_service.return_value = {"id": "svc-1"}
    c.list_machine_cameras.return_value = []
    c.get_pending_commands.return_value = []
    return c


def _cfg(camera_id="cam-1", machine_name="box", heartbeat_interval=30):
    return SimpleNamespace(
        camera_id=camera_id,
        machine_name=machine_name,
        heartbeat_interval=heartbeat_interval,
    )


def _link(client=None, *, machine_id=None, **cfg):
    client = client or _client()
    return TTTDeviceLink(client, _cfg(**cfg), machine_id=machine_id), client


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_serves_only_when_ttt_is_enabled_and_shares_the_client():
    client = object()
    on = IntegrationContext(
        SimpleNamespace(ttt=SimpleNamespace(enabled=True, heartbeat_interval=45)),
        ".",
        {"ttt": client},
    )
    link = TTTDeviceLink.from_context(on)
    assert isinstance(link, TTTDeviceLink)
    assert link._client is client
    assert link.heartbeat_interval == 45

    off = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert TTTDeviceLink.from_context(off) is None


# ---------------------------------------------------------------------------
# register -> register_service, once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_announces_the_service_once():
    link, client = _link()
    await link.register(CAPS)
    await link.register(CAPS)
    client.register_service.assert_called_once_with("box", CAPS)


@pytest.mark.asyncio
async def test_register_falls_back_to_the_host_name():
    link, client = _link(machine_name="")
    with patch(
        "video_grouper.integrations.device_link.sources.ttt.platform.node",
        return_value="host-7",
    ):
        await link.register(CAPS)
    client.register_service.assert_called_once_with("host-7", CAPS)


@pytest.mark.asyncio
async def test_register_waits_for_sign_in():
    client = _client(signed_in=False)
    link, _ = _link(client)
    await link.register(CAPS)
    client.register_service.assert_not_called()
    client.ensure_signed_in.return_value = True
    await link.register(CAPS)
    client.register_service.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, {}, RuntimeError("down")])
async def test_register_retries_until_it_gets_an_id(outcome, caplog):
    client = _client()
    client.register_service.side_effect = [outcome, {"id": "svc-1"}]
    link, _ = _link(client)
    with caplog.at_level(logging.ERROR):
        await link.register(CAPS)  # never raises
    await link.register(CAPS)
    await link.register(CAPS)
    assert client.register_service.call_count == 2
    if isinstance(outcome, Exception):
        assert "failed to register service" in caplog.text


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_heartbeat_only_once_registered_and_signed_in():
    link, client = _link()
    await link.heartbeat()
    client.send_heartbeat.assert_not_called()  # not registered yet

    await link.register(CAPS)
    await link.heartbeat()
    client.send_heartbeat.assert_called_once_with("svc-1", "online")

    client.ensure_signed_in.return_value = False
    await link.heartbeat()
    client.send_heartbeat.assert_called_once()  # signed out: skipped


@pytest.mark.asyncio
async def test_health_goes_through_the_enhanced_heartbeat_instead():
    from video_grouper.integrations.device_link import DeviceHealth

    link, client = _link()
    await link.register(CAPS)
    await link.heartbeat(
        DeviceHealth(cpu_usage_percent=12.5, version="1.2.3", error_count_24h=0)
    )
    client.enhanced_heartbeat.assert_called_once_with(
        "svc-1", {"cpu_usage_percent": 12.5, "version": "1.2.3", "error_count_24h": 0}
    )
    client.send_heartbeat.assert_not_called()  # enhanced also marks it online


@pytest.mark.asyncio
async def test_empty_health_falls_back_to_the_plain_heartbeat():
    from video_grouper.integrations.device_link import DeviceHealth

    link, client = _link()
    await link.register(CAPS)
    await link.heartbeat(DeviceHealth())
    client.enhanced_heartbeat.assert_not_called()
    client.send_heartbeat.assert_called_once_with("svc-1", "online")


@pytest.mark.asyncio
async def test_camera_status_online_and_commands_for_the_camera_id():
    link, client = _link(camera_id="cam-9")
    await link.heartbeat()
    client.update_camera_status.assert_called_once_with(
        camera_id="cam-9", status="online"
    )
    client.get_pending_commands.assert_called_once_with("cam-9")


@pytest.mark.asyncio
async def test_no_camera_id_no_camera_calls():
    link, client = _link(camera_id="")
    assert await link.heartbeat() == HeartbeatReply()
    client.update_camera_status.assert_not_called()
    client.get_pending_commands.assert_not_called()


@pytest.mark.asyncio
async def test_configured_camera_id_is_the_fallback_until_registration():
    link, client = _link(camera_id="cam-typed")
    await link.heartbeat()
    client.update_camera_status.assert_called_once_with(
        camera_id="cam-typed", status="online"
    )


# ---------------------------------------------------------------------------
# register_cameras
# ---------------------------------------------------------------------------

CAMS = [
    DeviceCamera("front", "SER-1", "reolink", "RLC-81MA", "AA:BB:CC:00:11:22"),
    DeviceCamera("no-serial", "", "dahua"),
]


@pytest.mark.asyncio
async def test_registers_machine_and_cameras_and_heartbeats_each():
    client = _client()
    client.register_device.return_value = {
        "machine_id": "me",
        "cameras": [{"name": "front", "serial": "SER-1", "camera_id": "c-1"}],
    }
    link, _ = _link(client, machine_id="me", camera_id="cam-typed", machine_name="box")
    assert await link.register_cameras(CAMS) == {"front": "c-1"}
    machine_id, machine_name, cameras = client.register_device.call_args.args
    assert (machine_id, machine_name) == ("me", "box")
    assert cameras == [  # serial-less cameras are not sent
        {
            "name": "front",
            "serial": "SER-1",
            "camera_type": "reolink",
            "model": "RLC-81MA",
            "mac_address": "AA:BB:CC:00:11:22",
        }
    ]
    await link.heartbeat()
    client.update_camera_status.assert_called_once_with(
        camera_id="c-1", status="online"
    )  # the registered id replaces the hand-typed one
    client.get_pending_commands.assert_called_once_with("c-1")


@pytest.mark.asyncio
async def test_registration_needs_a_session_and_a_machine_id():
    signed_out = _client(signed_in=False)
    link, _ = _link(signed_out, machine_id="me")
    assert await link.register_cameras(CAMS) == {}
    no_machine, client = _link(machine_id=None)
    assert await no_machine.register_cameras(CAMS) == {}
    signed_out.register_device.assert_not_called()
    client.register_device.assert_not_called()


@pytest.mark.asyncio
async def test_registration_errors_are_swallowed():
    client = _client()
    client.register_device.side_effect = RuntimeError("404")
    link, _ = _link(client, machine_id="me")
    assert await link.register_cameras(CAMS) == {}


@pytest.mark.asyncio
async def test_commands_carry_the_camera_they_target():
    client = _client()
    client.register_device.return_value = {
        "cameras": [{"name": "front", "camera_id": "c-1"}]
    }
    client.get_pending_commands.return_value = [
        {"id": "k-1", "command_type": "restart", "parameters": {}}
    ]
    link, _ = _link(client, machine_id="me")
    await link.register_cameras(CAMS)
    (command,) = (await link.heartbeat()).commands
    assert (command.id, command.camera) == ("k-1", "front")


@pytest.mark.asyncio
async def test_camera_state_is_this_machines_enabled_flag_per_camera():
    client = _client()
    client.list_machine_cameras.return_value = [
        {
            "camera_name": "front",
            "machines": [
                {"machine_id": "other", "enabled": True},
                {"machine_id": "me", "enabled": True},
            ],
        },
        {  # this machine not listed (unregistered here): no say -> stays enabled
            "camera_name": "back",
            "machines": [{"machine_id": "other", "enabled": True}],
        },
        {  # this machine listed, not enabled
            "camera_name": "side",
            "machines": [{"machine_id": "me", "enabled": False}],
        },
        {"camera_name": "lonely", "machines": []},
        {"camera_name": "", "machines": [{"machine_id": "me", "enabled": True}]},
    ]
    link, _ = _link(client, machine_id="me")
    reply = await link.heartbeat()
    assert reply.camera_enabled == {"front": True, "side": False}


@pytest.mark.asyncio
async def test_no_machine_id_no_camera_state():
    link, client = _link(machine_id=None)
    assert (await link.heartbeat()).camera_enabled is None
    client.list_machine_cameras.assert_not_called()


@pytest.mark.asyncio
async def test_machine_id_is_looked_up_once_from_storage(tmp_path):
    client = _client()
    link = TTTDeviceLink(client, _cfg(), str(tmp_path))
    with patch(
        "video_grouper.utils.machine_id.get_or_create_machine_id",
        return_value="m-1",
    ) as lookup:
        await link.heartbeat()
        await link.heartbeat()
    lookup.assert_called_once_with(str(tmp_path))
    assert client.list_machine_cameras.call_count == 2


@pytest.mark.asyncio
async def test_machine_id_lookup_failure_means_no_camera_state():
    client = _client()
    link = TTTDeviceLink(client, _cfg(), ".")
    with patch(
        "video_grouper.utils.machine_id.get_or_create_machine_id",
        side_effect=OSError("read-only"),
    ) as lookup:
        assert (await link.heartbeat()).camera_enabled is None
        await link.heartbeat()
    lookup.assert_called_once()  # not retried
    client.list_machine_cameras.assert_not_called()


@pytest.mark.asyncio
async def test_pending_commands_become_device_commands(caplog):
    client = _client()
    client.get_pending_commands.return_value = [
        {"id": "c1", "command_type": "restart", "parameters": {"a": 1}},
        {"id": "c2", "command_type": "delete_old_files", "parameters": None},
        {"command_type": "restart"},  # no id: skipped
    ]
    link, _ = _link(client)
    with caplog.at_level(logging.INFO):
        reply = await link.heartbeat()
    assert reply.commands == [
        DeviceCommand("c1", "restart", {"a": 1}),
        DeviceCommand("c2", "delete_old_files", {}),
    ]
    assert "Found 3 pending command(s)" in caplog.text


@pytest.mark.asyncio
async def test_each_heartbeat_call_fails_on_its_own(caplog):
    client = _client()
    client.send_heartbeat.side_effect = RuntimeError("svc down")
    client.update_camera_status.side_effect = RuntimeError("status down")
    client.list_machine_cameras.return_value = [
        {"camera_name": "front", "machines": [{"machine_id": "me", "enabled": True}]}
    ]
    client.get_pending_commands.return_value = [{"id": "c1", "command_type": "x"}]
    link, _ = _link(client, machine_id="me")
    await link.register(CAPS)
    with caplog.at_level(logging.WARNING):
        reply = await link.heartbeat()
    assert reply.camera_enabled == {"front": True}
    assert [c.id for c in reply.commands] == ["c1"]
    assert "Heartbeat failed: status down" in caplog.text


@pytest.mark.asyncio
async def test_lookup_failures_mean_no_news_and_no_commands(caplog):
    client = _client()
    client.list_machine_cameras.side_effect = RuntimeError("down")
    client.get_pending_commands.side_effect = RuntimeError("down")
    link, _ = _link(client, machine_id="me")
    with caplog.at_level(logging.WARNING):
        assert await link.heartbeat() == HeartbeatReply()
    assert "Failed to sync camera enabled state" in caplog.text
    assert "Failed to poll commands" in caplog.text
    client.update_camera_status.assert_called_once()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_and_complete_map_to_the_client():
    link, client = _link()
    await link.acknowledge_command("c1")
    await link.complete_command("c1", {"success": True, "message": "ok"})
    client.acknowledge_command.assert_called_once_with("c1")
    client.complete_command.assert_called_once_with(
        "c1", {"success": True, "message": "ok"}
    )


@pytest.mark.asyncio
async def test_command_reports_never_raise(caplog):
    client = _client()
    client.acknowledge_command.side_effect = RuntimeError("down")
    client.complete_command.side_effect = RuntimeError("down")
    link, _ = _link(client)
    with caplog.at_level(logging.WARNING):
        await link.acknowledge_command("c1")
        await link.complete_command("c1", {"success": False, "message": "x"})
    assert "Failed to acknowledge command" in caplog.text
    assert "Failed to complete command" in caplog.text


@pytest.mark.asyncio
async def test_stitch_profiles_come_from_each_cameras_config():
    client = _client()
    client.register_device.return_value = {
        "cameras": [{"name": "front", "camera_id": "c-1"}]
    }
    client.get_camera_config.return_value = {"stitch_profile": {"seam_x": 1}}
    link, _ = _link(client, machine_id="me")
    await link.register_cameras(CAMS)
    assert await link.stitch_profiles() == {"front": {"seam_x": 1}}
    client.get_camera_config.assert_called_with("c-1")


@pytest.mark.asyncio
async def test_no_stitch_profiles_while_signed_out_or_on_error():
    link, client = _link(_client(signed_in=False))
    assert await link.stitch_profiles() == {}
    client.get_camera_config.assert_not_called()
    failing = _client()
    failing.get_camera_config.side_effect = RuntimeError("down")
    link, _ = _link(failing)
    assert await link.stitch_profiles() == {}


@pytest.mark.asyncio
async def test_a_camera_ttt_refuses_gets_no_id(caplog):
    client = _client()
    client.register_device.return_value = {
        "cameras": [
            {
                "name": "front",
                "serial": "SER-1",
                "camera_id": "c-1",
                "error": "MAC does not match the registered camera",
            }
        ]
    }
    link, _ = _link(client, machine_id="me", camera_id="")
    with caplog.at_level(logging.WARNING):
        assert await link.register_cameras(CAMS) == {}
    assert "MAC does not match" in caplog.text


# ---------------------------------------------------------------------------
# An expired access token is refreshed, not read as signed out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_token_is_refreshed_and_the_service_registers(expired_ttt):
    expired_ttt.routes[("POST", "/api/internal/device-link/register-service")] = {
        "id": "svc-1"
    }
    link = TTTDeviceLink(expired_ttt.client, _cfg(camera_id=""), machine_id=None)
    await link.register(CAPS)
    assert link._service_id == "svc-1"
    assert expired_ttt.refreshes == 1


@pytest.mark.asyncio
async def test_signed_out_heartbeat_sends_nothing_and_reports_no_news():
    link, client = _link(_client(signed_in=False), machine_id="me")
    assert await link.heartbeat() == HeartbeatReply()
    client.update_camera_status.assert_not_called()
    client.list_machine_cameras.assert_not_called()
    client.get_pending_commands.assert_not_called()


def test_registers_cameras_only_with_a_machine_id_and_a_session():
    link, client = _link(machine_id="me")
    client.has_session.return_value = True
    assert link.registers_cameras is True
    client.has_session.return_value = False
    assert link.registers_cameras is False
    no_machine, client = _link(machine_id=None)
    client.has_session.return_value = True
    assert no_machine.registers_cameras is False


# ---------------------------------------------------------------------------
# report_stuck_downloads -> report_stuck_downloads(machine_id, rows)
# ---------------------------------------------------------------------------


def _stuck(camera="front", **kw):
    from video_grouper.integrations.device_link import StuckDownload

    fields = {
        "camera": camera,
        "group": "2026.07.01-10.00.00",
        "file_name": "a.mp4",
        "recorded_at": "2026-07-01T10:00:00",
        "failed_attempts": 2,
        "error": "timed out",
        "last_failed_at": "2026-07-01T12:00:00",
    }
    fields.update(kw)
    return StuckDownload(**fields)


def _stuck_link(client=None, *, machine_id="me", ids=None):
    from video_grouper.integrations.ttt_cameras import TTTCameraIds

    cameras = TTTCameraIds(None)
    cameras.update(ids if ids is not None else {"front": "c-1", "back": "c-2"})
    client = client or _client()
    link = TTTDeviceLink(client, _cfg(), machine_id=machine_id, camera_ids=cameras)
    return link, client


@pytest.mark.asyncio
async def test_stuck_downloads_go_out_under_ttt_camera_ids():
    link, client = _stuck_link()
    assert await link.report_stuck_downloads([_stuck(), _stuck("back", error=None)])
    client.report_stuck_downloads.assert_called_once_with(
        "me",
        [
            {
                "camera_id": "c-1",
                "group_dir": "2026.07.01-10.00.00",
                "file_name": "a.mp4",
                "recorded_at": "2026-07-01T10:00:00",
                "failed_attempts": 2,
                "error_message": "timed out",
                "last_failed_at": "2026-07-01T12:00:00",
            },
            {
                "camera_id": "c-2",
                "group_dir": "2026.07.01-10.00.00",
                "file_name": "a.mp4",
                "recorded_at": "2026-07-01T10:00:00",
                "failed_attempts": 2,
                "error_message": None,
                "last_failed_at": "2026-07-01T12:00:00",
            },
        ],
    )


@pytest.mark.asyncio
async def test_stuck_downloads_without_a_ttt_camera_are_left_out():
    link, client = _stuck_link()
    await link.report_stuck_downloads(
        [_stuck("unregistered"), _stuck(None), _stuck("front")]
    )
    (_, rows), _ = client.report_stuck_downloads.call_args
    assert [r["camera_id"] for r in rows] == ["c-1"]


@pytest.mark.asyncio
async def test_an_empty_list_is_sent_to_clear_ttts():
    link, client = _stuck_link()
    assert await link.report_stuck_downloads([]) is True
    client.report_stuck_downloads.assert_called_once_with("me", [])


@pytest.mark.asyncio
async def test_stuck_download_fields_fit_ttts_limits():
    link, client = _stuck_link()
    await link.report_stuck_downloads(
        [
            _stuck(
                error="x" * 900,
                recorded_at="not a time",
                last_failed_at=None,
                failed_attempts=-1,
            ),
            _stuck(file_name="f" * 256),  # longer than TTT takes: left out
            _stuck(group=""),
        ]
    )
    ((_, rows), _) = client.report_stuck_downloads.call_args
    (row,) = rows
    assert row["error_message"] == "x" * 500
    assert row["recorded_at"] is None and row["last_failed_at"] is None
    assert row["failed_attempts"] == 0


@pytest.mark.asyncio
async def test_at_most_500_stuck_downloads_are_sent():
    link, client = _stuck_link()
    await link.report_stuck_downloads(
        [_stuck(file_name=f"{i:04}.mp4") for i in range(600)]
    )
    ((_, rows), _) = client.report_stuck_downloads.call_args
    assert len(rows) == 500
    assert rows[0]["file_name"] == "0000.mp4"  # oldest first, as listed


@pytest.mark.asyncio
async def test_stuck_downloads_need_a_session_and_a_machine_id():
    signed_out, client = _stuck_link(_client(signed_in=False))
    assert await signed_out.report_stuck_downloads([_stuck()]) is False
    client.report_stuck_downloads.assert_not_called()
    no_machine, client = _stuck_link(machine_id=None)
    assert await no_machine.report_stuck_downloads([_stuck()]) is False
    client.report_stuck_downloads.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_stuck_report_is_swallowed_and_logged_once(caplog):
    client = _client()
    client.report_stuck_downloads.side_effect = RuntimeError("404 machine")
    link, _ = _stuck_link(client)
    with caplog.at_level(logging.WARNING):
        assert await link.report_stuck_downloads([_stuck()]) is False
        assert await link.report_stuck_downloads([_stuck()]) is False
    warnings = [r for r in caplog.records if "stuck downloads" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "404 machine" in warnings[0].getMessage()

    client.report_stuck_downloads.side_effect = None
    caplog.clear()
    assert await link.report_stuck_downloads([_stuck()]) is True
    client.report_stuck_downloads.side_effect = RuntimeError("down again")
    with caplog.at_level(logging.WARNING):
        await link.report_stuck_downloads([_stuck()])
    assert "down again" in caplog.text  # a new failure after a success is news


# ---------------------------------------------------------------------------
# Commands addressed to an install (machine_id): several installs can share
# one camera, so another install's command is left pending for it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commands_for_another_install_are_neither_returned_nor_acknowledged():
    client = _client()
    client.get_pending_commands.return_value = [
        {
            "id": "theirs",
            "command_type": "skip_download",
            "parameters": {"machine_id": "other-machine", "group_dir": "g"},
        },
        {
            "id": "mine",
            "command_type": "skip_download",
            "parameters": {"machine_id": "ME-UUID", "group_dir": "g"},
        },
        {"id": "anyone", "command_type": "restart", "parameters": {}},
    ]
    link, _ = _link(client, machine_id="me-uuid")
    reply = await link.heartbeat()
    assert [c.id for c in reply.commands] == ["mine", "anyone"]
    client.acknowledge_command.assert_not_called()


@pytest.mark.asyncio
async def test_without_a_machine_id_addressed_commands_are_not_ours():
    client = _client()
    client.get_pending_commands.return_value = [
        {"id": "addressed", "command_type": "x", "parameters": {"machine_id": "m"}},
        {"id": "plain", "command_type": "x", "parameters": None},
    ]
    link, _ = _link(client, machine_id=None)
    assert [c.id for c in (await link.heartbeat()).commands] == ["plain"]


@pytest.mark.asyncio
async def test_an_unnamed_stuck_download_goes_under_the_only_camera():
    """Old state.json entries carry no camera name; with one camera it can only be that one."""
    link, client = _stuck_link(ids={"front": "c-1"})
    await link.report_stuck_downloads([_stuck(None)])
    (_, rows), _ = client.report_stuck_downloads.call_args
    assert [r["camera_id"] for r in rows] == ["c-1"]
