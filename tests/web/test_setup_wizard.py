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
    assert resp.headers["location"] == "/setup/youtube"

    # YouTube step — skip without configuring (this fast path is the
    # one users will hit if they don't have GCP credentials handy yet;
    # they can always come back via the dashboard's YouTube section).
    resp = client.post("/setup/youtube/skip", follow_redirects=False)
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
    # Wizard must mark onboarding complete; otherwise dashboards keep
    # bouncing the user back to /setup forever.
    assert cfg.setup.onboarding_completed is True


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


# ── Re-running the wizard must not reset what it never asks about ──────


class _State:
    """The six things the wizard collects."""

    storage_path = "/shared_data"
    camera_type = "reolink"
    camera_name = "field"
    camera_ip = "192.168.86.24"
    camera_username = "admin"
    camera_password = "pw"
    is_complete = True


def _configured() -> object:
    """A Config that looks like an install someone has actually set up."""
    from video_grouper.utils.config import Config
    from video_grouper.web.setup.router import _build_config

    base = _build_config(_State())
    data = base.model_dump(by_alias=True)
    # Things the wizard never asks about.
    data["NTFY"]["enabled"] = True
    data["NTFY"]["server_url"] = "https://ntfy.example.invalid"
    data["TTT"]["enabled"] = True
    data["YOUTUBE"]["privacy_status"] = "public"
    data["YOUTUBE"]["processed_playlist"] = {
        "name_format": "Flash 2013 highlights",
        "description": "Season highlights",
        "privacy_status": "unlisted",
    }
    data["STORAGE"]["min_free_gb"] = 42.0
    data["PROCESSING"]["max_concurrent_downloads"] = 7
    return Config.model_validate(data, by_alias=True, by_name=True)


def test_rerunning_setup_keeps_integrations_it_never_asks_about():
    """The wizard collects six values; it must not reset everything else."""
    from video_grouper.web.setup.router import _build_config

    merged = _build_config(_State(), _configured())

    assert merged.ntfy.enabled is True
    assert merged.ntfy.server_url == "https://ntfy.example.invalid"
    assert merged.ttt.enabled is True
    assert merged.processing.max_concurrent_downloads == 7


def test_rerunning_setup_keeps_the_parts_of_sections_it_only_touches():
    """STORAGE.path and YOUTUBE.enabled are written; their siblings are not."""
    from video_grouper.web.setup.router import _build_config

    merged = _build_config(_State(), _configured())

    assert merged.storage.path == "/shared_data"
    assert merged.storage.min_free_gb == 42.0, "replacing the section reset this"
    assert merged.youtube.privacy_status == "public"
    assert merged.youtube.processed_playlist is not None
    assert merged.youtube.processed_playlist.name_format == "Flash 2013 highlights"


def test_a_second_camera_survives_rerunning_setup():
    """The wizard configures one camera; it must not delete the others."""
    from video_grouper.utils.config import CameraConfig, Config
    from video_grouper.web.setup.router import _build_config

    data = _configured().model_dump(by_alias=True)
    data["cameras"].append(
        CameraConfig(
            name="end-line",
            type="dahua",
            device_ip="192.168.86.60",
            username="admin",
            password="pw",
        ).model_dump()
    )
    existing = Config.model_validate(data, by_alias=True, by_name=True)

    merged = _build_config(_State(), existing)
    names = sorted(c.name for c in merged.cameras)
    assert names == ["end-line", "field"]


def test_rerunning_setup_updates_the_camera_rather_than_duplicating_it():
    """Same name, new address -- one entry, not two."""
    from video_grouper.web.setup.router import _build_config

    class Moved(_State):
        camera_ip = "192.168.86.99"

    merged = _build_config(Moved(), _configured())
    field = [c for c in merged.cameras if c.name == "field"]
    assert len(field) == 1
    assert field[0].device_ip == "192.168.86.99"


def test_a_configured_pipeline_is_not_reseeded():
    """Re-running setup must not throw away a pipeline the user wired up."""
    from video_grouper.web.setup.router import _build_config

    existing = _configured()
    before = existing.pipeline.model_dump()
    before_steps = list(before.get("steps") or [])
    assert before_steps, "fixture should carry a seeded pipeline"

    merged = _build_config(_State(), existing)
    assert merged.pipeline.model_dump().get("steps") == before_steps


def test_a_fresh_install_still_gets_a_full_config():
    """With nothing on disk the wizard still produces every section."""
    from video_grouper.web.setup.router import _build_config

    fresh = _build_config(_State(), None)
    assert fresh.setup.onboarding_completed is True
    assert fresh.storage.path == "/shared_data"
    assert [c.name for c in fresh.cameras] == ["field"]
    assert fresh.pipeline.model_dump().get("steps"), "fresh install gets a scaffold"


def test_finishing_the_wizard_on_a_configured_install_merges_the_file(
    client, config_path, tmp_path
):
    """End to end through the route, against a real config.ini on disk.

    Walks storage -> camera -> finish with a config already written, then
    re-reads the file. This is the path that actually persists, so it is the
    one that has to be proved: before merging, this reset NTFY and TTT.
    """
    from video_grouper.utils.config import save_config
    from video_grouper.web.setup.router import _build_config

    # An install someone has already configured.
    class _S:
        storage_path = str(tmp_path / "data")
        camera_type = "dahua"
        camera_name = "field"
        camera_ip = "192.168.1.10"
        camera_username = "admin"
        camera_password = "pw"
        is_complete = True

    existing = _build_config(_S())
    existing.ntfy.enabled = True
    existing.ntfy.server_url = "https://ntfy.example.invalid"
    existing.ttt.enabled = True
    existing.storage.min_free_gb = 42.0
    save_config(existing, config_path)

    # Now re-run the wizard, pointing the camera somewhere new.
    client.get("/setup/welcome")
    client.post("/setup/storage", data={"storage_path": str(tmp_path / "data")})
    client.post(
        "/setup/camera",
        data={
            "camera_type": "reolink",
            "camera_name": "field",
            "camera_ip": "192.168.86.24",
            "camera_username": "admin",
            "camera_password": "newpw",
        },
    )
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 303, resp.text

    after = load_config(config_path)
    # The wizard's own answers landed.
    assert after.setup.onboarding_completed is True
    assert [c.device_ip for c in after.cameras] == ["192.168.86.24"]
    assert after.cameras[0].type == "reolink"
    # And nothing it never asked about was reset.
    assert after.ntfy.enabled is True
    assert after.ntfy.server_url == "https://ntfy.example.invalid"
    assert after.ttt.enabled is True
    assert after.storage.min_free_gb == 42.0
