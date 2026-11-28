"""NullDeviceLink (TTT off): always available; sends nothing, every camera
stays enabled (the reply says nothing) and no commands ever arrive."""

from types import SimpleNamespace

import pytest

from video_grouper.integrations.device_link import HeartbeatReply
from video_grouper.integrations.device_link.sources.null import NullDeviceLink
from video_grouper.integrations.registry import IntegrationContext


def test_always_available():
    ctx = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert isinstance(NullDeviceLink.from_context(ctx), NullDeviceLink)


@pytest.mark.asyncio
async def test_every_verb_is_a_no_op_and_the_reply_says_nothing():
    link = NullDeviceLink()
    assert await link.register({"ffmpeg": True}) is None
    reply = await link.heartbeat()
    assert reply == HeartbeatReply()
    assert reply.camera_enabled is None and reply.commands == []
    assert await link.acknowledge_command("cmd-1") is None
    assert await link.complete_command("cmd-1", {"success": True}) is None


def test_default_heartbeat_cadence():
    assert NullDeviceLink().heartbeat_interval == 30


def test_does_not_register_cameras():
    # The processor then never asks the cameras to describe themselves.
    assert NullDeviceLink().registers_cameras is False
