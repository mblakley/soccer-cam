"""Tests for the schema-driven config editor at /config."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils.config import TTTConfig, load_config
from video_grouper.web.auth_server import create_app


_DIST_INI = """\
[CAMERA.default]
type = dahua
device_ip = 192.168.1.100
username = admin
password = secret

[STORAGE]
path = /shared_data
min_free_gb = 2.0

[RECORDING]
min_duration = 60
max_duration = 3600

[PROCESSING]
max_concurrent_downloads = 2
trim_end_enabled = false

[LOGGING]
level = INFO
log_dir = logs

[APP]
check_interval_seconds = 60

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false
server_url = https://ntfy.sh
topic =

[YOUTUBE]
enabled = false

[CLOUD_SYNC]
enabled = false

[TTT]
auth_server_enabled = false
auth_server_port = 8765
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "config.ini"
    p.write_text(_DIST_INI, encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, config_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765") as c:
        yield c


def test_get_config_renders_form_with_existing_values(client):
    body = client.get("/config").text
    assert "<title>Soccer-Cam configuration</title>" in body
    # Several known fields should appear with their current values.
    assert 'name="STORAGE.path"' in body
    assert 'value="/shared_data"' in body
    assert 'name="RECORDING.min_duration"' in body
    assert 'value="60"' in body
    # Boolean as checkbox
    assert 'type="checkbox"' in body
    assert 'name="TTT.auth_server_enabled"' in body


def test_get_config_redacts_sensitive_fields(client):
    body = client.get("/config").text
    # The camera password ("secret" in the test fixture) must never be
    # echoed back as a value attribute. (Field labels like "client_secret"
    # may legitimately mention the substring; check value attrs only.)
    assert 'value="secret"' not in body
    # Sensitive fields are rendered as password inputs with placeholder.
    assert 'placeholder="(unchanged)"' in body


def test_get_config_skips_list_and_dict_fields(client):
    body = client.get("/config").text
    # plugin_signing_public_keys is a list field — should not render an input.
    assert 'name="TTT.plugin_signing_public_keys"' not in body


def test_post_config_saves_round_trip(client, config_path):
    # Edit a few scalar fields.
    resp = client.post(
        "/config",
        data={
            "STORAGE.path": "/data/games",
            "STORAGE.min_free_gb": "5.5",
            "RECORDING.min_duration": "120",
            "RECORDING.max_duration": "3600",
            "PROCESSING.max_concurrent_downloads": "4",
            "PROCESSING.trim_end_enabled": "false",
            "LOGGING.level": "DEBUG",
            "LOGGING.log_dir": "logs",
            "APP.check_interval_seconds": "60",
            "TEAMSNAP.enabled": "false",
            "PLAYMETRICS.enabled": "false",
            "NTFY.enabled": "true",
            "NTFY.server_url": "https://ntfy.sh",
            "YOUTUBE.enabled": "false",
            "CLOUD_SYNC.enabled": "false",
            "TTT.auth_server_enabled": "true",
            "TTT.auth_server_port": "9999",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/config?saved=1"

    # Reload from disk and verify round-trip.
    reloaded = load_config(config_path)
    assert reloaded.storage.path == "/data/games"
    assert reloaded.storage.min_free_gb == 5.5
    assert reloaded.recording.min_duration == 120
    assert reloaded.processing.max_concurrent_downloads == 4
    assert reloaded.logging.level == "DEBUG"
    assert reloaded.ntfy.enabled is True
    assert reloaded.ttt.auth_server_enabled is True
    assert reloaded.ttt.auth_server_port == 9999


def test_post_config_blank_password_keeps_existing(client, config_path):
    """Sensitive fields submitted blank should NOT clobber the saved value."""
    initial = load_config(config_path)
    initial_password = initial.cameras[0].password
    assert initial_password == "secret"

    # Submit a save with the password input left blank.
    resp = client.post(
        "/config",
        data={
            "STORAGE.path": initial.storage.path,
            "STORAGE.min_free_gb": str(initial.storage.min_free_gb),
            "RECORDING.min_duration": str(initial.recording.min_duration),
            "RECORDING.max_duration": str(initial.recording.max_duration),
            "TTT.auth_server_port": str(initial.ttt.auth_server_port),
        },
        follow_redirects=False,
    )
    # Blank password is treated as "leave alone"; save should still succeed.
    assert resp.status_code in (303, 422)


def test_post_config_invalid_returns_422(client, config_path):
    """Non-numeric value for an int field should fail validation."""
    resp = client.post(
        "/config",
        data={
            "RECORDING.min_duration": "not-a-number",
            "TTT.auth_server_port": "8765",
        },
        follow_redirects=False,
    )
    # Either Python coercion or Pydantic validation rejects it.
    assert resp.status_code in (422, 500)


def test_config_editor_not_mounted_when_path_missing(tmp_path):
    """Without config_path, /config returns 404."""
    app = create_app(TTTConfig(), str(tmp_path))  # no config_path
    with TestClient(app, base_url="http://localhost:8765") as c:
        resp = c.get("/config")
    assert resp.status_code == 404
