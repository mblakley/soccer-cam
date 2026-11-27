"""TTTDeviceLink on its own: each verb maps onto the right TTT client calls
with the right arguments and gates (service calls only while signed in and
registered; camera status + commands only with a camera id; camera state only
with a machine id), and every call fails on its own without raising."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.api_integrations.ttt_api import TTTApiClient
from video_grouper.integrations.device_link import DeviceCommand, HeartbeatReply
from video_grouper.integrations.device_link.sources.ttt import TTTDeviceLink
from video_grouper.integrations.registry import IntegrationContext

CAPS = {"ffmpeg": True, "pipeline": False, "camera_type": "dahua", "camera_ip": "ip"}


def _client(signed_in=True):
    c = MagicMock(spec=TTTApiClient)
    c.is_authenticated.return_value = signed_in
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
    client.is_authenticated.return_value = True
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

    client.is_authenticated.return_value = False
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
async def test_camera_id_is_read_on_each_call():
    link, client = _link(camera_id="")
    await link.heartbeat()
    link._ttt_config.camera_id = "cam-new"
    await link.heartbeat()
    client.update_camera_status.assert_called_once_with(
        camera_id="cam-new", status="online"
    )


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
