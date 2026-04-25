"""Tests for the autocam_gui provider — adapter around run_autocam_on_file."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.ball_tracking.config import AutocamGuiProviderConfig
from video_grouper.ball_tracking.providers.autocam_gui import AutocamGuiProvider


@pytest.fixture
def context(tmp_path):
    return ProviderContext(
        group_dir=tmp_path / "flash__2024.06.01_vs_IYSA_home",
        team_name="flash",
        storage_path=tmp_path,
    )


@pytest.mark.asyncio
async def test_routes_to_run_autocam_on_file_via_executor(context):
    """Adapter must call run_autocam_on_file with the legacy AutocamConfig
    shape and the input/output paths.
    """
    cfg = AutocamGuiProviderConfig(executable="C:/Path/To/AutoCam.exe")
    provider = AutocamGuiProvider(cfg)

    with patch(
        "video_grouper.tray.autocam_automation.run_autocam_on_file",
        return_value=True,
    ) as mock_run:
        result = await provider.run("in.mp4", "out.mp4", context)

    assert result is True
    assert mock_run.call_count == 1
    legacy_cfg, in_path, out_path = mock_run.call_args.args
    assert legacy_cfg.enabled is True
    assert legacy_cfg.executable == "C:/Path/To/AutoCam.exe"
    assert in_path == "in.mp4"
    assert out_path == "out.mp4"


@pytest.mark.asyncio
async def test_returns_false_when_driver_returns_false(context):
    cfg = AutocamGuiProviderConfig(executable="x")
    provider = AutocamGuiProvider(cfg)

    with patch(
        "video_grouper.tray.autocam_automation.run_autocam_on_file",
        return_value=False,
    ):
        result = await provider.run("in.mp4", "out.mp4", context)

    assert result is False


@pytest.mark.asyncio
async def test_swallows_driver_exceptions_and_returns_false(context):
    cfg = AutocamGuiProviderConfig(executable="x")
    provider = AutocamGuiProvider(cfg)

    with patch(
        "video_grouper.tray.autocam_automation.run_autocam_on_file",
        side_effect=RuntimeError("pywinauto exploded"),
    ):
        result = await provider.run("in.mp4", "out.mp4", context)

    # Provider contract: don't raise on expected failure modes; log + return False.
    assert result is False


def test_provider_self_registers_under_autocam_gui_name():
    # Importing the module above triggers register_provider('autocam_gui', ...).
    from video_grouper.ball_tracking import _PROVIDER_REGISTRY

    assert "autocam_gui" in _PROVIDER_REGISTRY
