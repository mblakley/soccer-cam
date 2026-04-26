"""Tests for [NODE].role enforcement in VideoGrouperApp."""

from __future__ import annotations

import pytest

from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.utils.config import (
    AppConfig,
    AutocamConfig,
    CameraConfig,
    CloudSyncConfig,
    Config,
    LoggingConfig,
    NodeConfig,
    NtfyConfig,
    PlayMetricsConfig,
    ProcessingConfig,
    RecordingConfig,
    StorageConfig,
    TeamSnapConfig,
    YouTubeConfig,
)


def _config(temp_storage, role: str = "standalone") -> Config:
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="192.168.1.100",
                username="admin",
                password="p",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(check_interval_seconds=1),
        teamsnap=TeamSnapConfig(enabled=False),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(enabled=False),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://x", topic="t"),
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
        node=NodeConfig(role=role),
    )


def test_standalone_role_starts_orchestrator(tmp_path):
    """Standalone is the default; orchestrator should initialize normally."""
    cfg = _config(str(tmp_path), role="standalone")
    app = VideoGrouperApp(cfg)
    assert app.config.node.role == "standalone"
    # Sanity: the regular processor list was built.
    assert app.upload_processor is not None


def test_master_role_starts_orchestrator(tmp_path):
    """Master is just standalone + the worker API; orchestrator still starts."""
    cfg = _config(str(tmp_path), role="master")
    app = VideoGrouperApp(cfg)
    assert app.config.node.role == "master"
    assert app.upload_processor is not None


def test_worker_role_refused_in_orchestrator(tmp_path):
    """Worker mode requires `python -m video_grouper.worker`, not the
    orchestrator entry point. VideoGrouperApp should refuse to start."""
    cfg = _config(str(tmp_path), role="worker")
    with pytest.raises(RuntimeError, match="role = 'worker'"):
        VideoGrouperApp(cfg)


def test_node_config_defaults():
    """Default role is standalone; no master URL; reasonable capabilities."""
    nc = NodeConfig()
    assert nc.role == "standalone"
    assert nc.master_url == ""
    assert "combine" in nc.capabilities
    assert "ball_tracking" in nc.capabilities


def test_node_capabilities_round_trip_from_string():
    """Capabilities written by save_config (as `str(list)`) read back."""
    nc = NodeConfig.model_validate(
        {"role": "worker", "capabilities": "['combine', 'trim']"}
    )
    assert nc.capabilities == ["combine", "trim"]
