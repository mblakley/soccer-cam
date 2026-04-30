"""Tests for the onboarding wizard at /setup/*."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils.config import TTTConfig, load_config
from video_grouper.web.auth_server import create_app


# Same-origin Origin header so auth_server's middleware accepts the
# wizard's POSTs (host_and_origin_check rejects state-changing requests
# with no Origin/Referer).
_SAME_ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture
def config_path(tmp_path):
    # Wizard is the path users hit when there's no config yet, so the
    # fixture intentionally doesn't create one upfront.
    return tmp_path / "config.ini"


@pytest.fixture
def client(tmp_path, config_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


def test_setup_root_redirects_to_welcome(client):
    resp = client.get("/setup/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/welcome"


def test_welcome_renders_and_sets_cookie(client):
    resp = client.get("/setup/welcome")
    assert resp.status_code == 200
    assert "Welcome to Soccer-Cam" in resp.text
    # Wizard cookie set so subsequent steps share state.
    assert "soccer_cam_wizard" in resp.headers.get("set-cookie", "")


def test_full_flow_persists_config(client, config_path):
    # Welcome → primes the cookie.
    client.get("/setup/welcome")

    # Storage step
    resp = client.post(
        "/setup/storage",
        data={"storage_path": "/data/games"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/camera"

    # Camera step
    resp = client.post(
        "/setup/camera",
        data={
            "camera_type": "dahua",
            "camera_name": "default",
            "camera_ip": "192.168.1.50",
            "camera_username": "admin",
            "camera_password": "hunter2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/summary"

    # Summary renders the entered values
    resp = client.get("/setup/summary")
    assert resp.status_code == 200
    assert "/data/games" in resp.text
    assert "192.168.1.50" in resp.text

    # Finish → config written + redirect to /config?saved=1
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/config?saved=1"

    # The wizard cookie was cleared on finish.
    cookie_header = resp.headers.get("set-cookie", "")
    assert "soccer_cam_wizard" in cookie_header
    assert ("Max-Age=0" in cookie_header) or ("expires=" in cookie_header.lower())

    # Config file was written and round-trips through load_config.
    assert config_path.exists()
    cfg = load_config(config_path)
    assert cfg.storage.path == "/data/games"
    assert len(cfg.cameras) == 1
    cam = cfg.cameras[0]
    assert cam.type == "dahua"
    assert cam.device_ip == "192.168.1.50"
    assert cam.username == "admin"
    assert cam.password == "hunter2"


def test_summary_redirects_when_state_incomplete(client):
    """Hitting /setup/summary directly without filling storage+camera
    bounces back to /setup/welcome instead of crashing or 500ing."""
    # No prior state: summary should redirect to welcome.
    resp = client.get("/setup/summary", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/welcome"


def test_finish_rejects_incomplete_state(client):
    """Finish with no state -> 400, not a half-written config.ini."""
    client.cookies.clear()
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 400


def test_setup_not_mounted_without_config_path(tmp_path):
    """Without a config_path, /setup/* is 404 (same gating as /config)."""
    app = create_app(TTTConfig(), str(tmp_path))
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        resp = c.get("/setup/welcome")
    assert resp.status_code == 404
