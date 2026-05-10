"""Tests for the YouTube OAuth wizard step + dashboard endpoints."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils.config import TTTConfig
from video_grouper.web import auth_server
from video_grouper.web.auth_server import (
    _read_youtube_status,
    _render_auth_flags_banner,
    _render_youtube_section,
    create_app,
)


_DESKTOP_CLIENT_SECRET = {
    "installed": {
        "client_id": "fake-client.apps.googleusercontent.com",
        "project_id": "fake-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "fake-secret-value",
        "redirect_uris": ["http://localhost"],
    }
}


@pytest.fixture
def storage(tmp_path):
    return tmp_path


@pytest.fixture
def client(storage, tmp_path):
    # Pass a config_path so the wizard router (`/setup/*`) is mounted —
    # otherwise GET /setup/youtube returns 404 in tests. The path
    # doesn't need to exist; the wizard creates it on /finish.
    config_path = tmp_path / "config.ini"
    app = create_app(TTTConfig(), str(storage), config_path=config_path)
    # The Origin/Referer middleware rejects POSTs without an origin
    # header. TestClient doesn't send one by default; we pin it to a
    # loopback host that's in the middleware's allow-list.
    with TestClient(
        app,
        base_url="http://localhost:8765",
        headers={"origin": "http://localhost:8765"},
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state_store():
    """Each test gets a fresh OAuth state store."""
    auth_server._yt_oauth_states.clear()
    yield
    auth_server._yt_oauth_states.clear()


def _write_client_secret(storage):
    yt_dir = storage / "youtube"
    yt_dir.mkdir(parents=True, exist_ok=True)
    (yt_dir / "client_secret.json").write_text(
        json.dumps(_DESKTOP_CLIENT_SECRET), encoding="utf-8"
    )


def _write_token(storage, *, refresh="r", expiry="2099-01-01T00:00:00Z"):
    yt_dir = storage / "youtube"
    yt_dir.mkdir(parents=True, exist_ok=True)
    (yt_dir / "token.json").write_text(
        json.dumps(
            {
                "token": "fake-access",
                "refresh_token": refresh,
                "client_id": "fake-client.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "expiry": expiry,
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
            }
        ),
        encoding="utf-8",
    )


# ----- _read_youtube_status ---------------------------------------------------


def test_read_status_missing_file(storage):
    s = _read_youtube_status(storage / "youtube" / "token.json")
    assert s["authorized"] is False
    assert s["identity"] is None


def test_read_status_no_refresh_token(storage):
    yt_dir = storage / "youtube"
    yt_dir.mkdir()
    (yt_dir / "token.json").write_text(json.dumps({"token": "x"}), encoding="utf-8")
    s = _read_youtube_status(yt_dir / "token.json")
    assert s["authorized"] is False


def test_read_status_full_token(storage):
    _write_token(storage)
    s = _read_youtube_status(storage / "youtube" / "token.json")
    assert s["authorized"] is True
    assert s["identity"] == "fake-client.apps.googleusercontent.com"


# ----- _render_youtube_section ------------------------------------------------


def test_render_section_no_credentials(storage):
    html = _render_youtube_section(storage)
    assert "not configured" in html.lower()
    assert "/auth/youtube/upload-credentials" in html
    assert "/setup/youtube" in html


def test_render_section_credentials_only(storage):
    _write_client_secret(storage)
    html = _render_youtube_section(storage)
    assert "Authorize" in html
    assert "/auth/youtube/start" in html


def test_render_section_authorized(storage):
    _write_client_secret(storage)
    _write_token(storage)
    html = _render_youtube_section(storage)
    assert "Authorized under" in html
    assert "fake-client" in html


# ----- _render_auth_flags_banner ----------------------------------------------


def test_banner_adds_youtube_reauth_button(storage):
    (storage / "youtube_auth_needed.json").write_text(
        json.dumps(
            {
                "provider": "youtube",
                "since": "2026-05-10T00:00:00Z",
                "last_error": "invalid_grant",
            }
        ),
        encoding="utf-8",
    )
    html = _render_auth_flags_banner(storage)
    assert "Re-authenticate" in html
    assert "/auth/youtube/start" in html


def test_banner_empty_when_no_flag(storage):
    assert _render_auth_flags_banner(storage) == ""


# ----- /auth/youtube/start ----------------------------------------------------


def test_start_400_without_client_secret(client):
    r = client.get("/auth/youtube/start", follow_redirects=False)
    assert r.status_code == 400
    assert "client_secret.json" in r.text


def test_start_redirects_to_google_with_state(client, storage):
    _write_client_secret(storage)
    r = client.get("/auth/youtube/start", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == "accounts.google.com"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["fake-client.apps.googleusercontent.com"]
    assert qs["state"]
    assert qs["redirect_uri"] == ["http://localhost:8765/auth/youtube/callback"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    state_token = qs["state"][0]
    assert state_token in auth_server._yt_oauth_states


def test_start_honors_return_to(client, storage):
    _write_client_secret(storage)
    r = client.get(
        "/auth/youtube/start?return_to=/setup/youtube", follow_redirects=False
    )
    state_token = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert auth_server._yt_oauth_states[state_token]["return_to"] == "/setup/youtube"


def test_start_rejects_external_return_to(client, storage):
    _write_client_secret(storage)
    r = client.get(
        "/auth/youtube/start?return_to=https://evil.example", follow_redirects=False
    )
    state_token = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    # Anything not starting with a single "/" gets normalized to "/"
    assert auth_server._yt_oauth_states[state_token]["return_to"] == "/"


# ----- /auth/youtube/callback -------------------------------------------------


def test_callback_rejects_missing_state(client):
    r = client.get("/auth/youtube/callback?code=abc", follow_redirects=False)
    assert r.status_code == 400
    assert "missing" in r.text.lower()


def test_callback_rejects_unknown_state(client):
    r = client.get(
        "/auth/youtube/callback?code=abc&state=nonexistent", follow_redirects=False
    )
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "tampered" in r.text.lower()


def test_callback_surfaces_google_error(client):
    r = client.get("/auth/youtube/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400
    assert "access_denied" in r.text


# ----- _yt_states_gc ----------------------------------------------------------


def test_state_gc_drops_expired_entries():
    auth_server._yt_oauth_states.clear()
    auth_server._yt_oauth_states["fresh"] = {
        "return_to": "/",
        "callback_uri": "x",
        "created_at": time.time(),
    }
    auth_server._yt_oauth_states["old"] = {
        "return_to": "/",
        "callback_uri": "x",
        "created_at": time.time() - auth_server._YT_STATE_TTL_SECONDS - 1,
    }
    auth_server._yt_states_gc()
    assert "fresh" in auth_server._yt_oauth_states
    assert "old" not in auth_server._yt_oauth_states


# ----- /auth/youtube/upload-credentials --------------------------------------


def test_upload_credentials_writes_file(client, storage):
    payload = json.dumps(_DESKTOP_CLIENT_SECRET).encode("utf-8")
    r = client.post(
        "/auth/youtube/upload-credentials",
        files={"client_secret": ("client_secret.json", payload, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    saved = (storage / "youtube" / "client_secret.json").read_bytes()
    assert json.loads(saved) == _DESKTOP_CLIENT_SECRET


def test_upload_credentials_rejects_garbage(client, storage):
    r = client.post(
        "/auth/youtube/upload-credentials",
        files={"client_secret": ("not-json.txt", b"this is not json", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert not (storage / "youtube" / "client_secret.json").exists()


def test_upload_credentials_rejects_wrong_shape(client, storage):
    bad = json.dumps({"installed": {"foo": "bar"}}).encode("utf-8")
    r = client.post(
        "/auth/youtube/upload-credentials",
        files={"client_secret": ("client_secret.json", bad, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "client_id" in r.text


# ----- /setup/youtube wizard step --------------------------------------------


def test_wizard_youtube_renders_upload_form_initially(client):
    r = client.get("/setup/youtube", follow_redirects=False)
    assert r.status_code == 200
    assert "client_secret.json" in r.text
    assert "/setup/youtube/upload" in r.text
