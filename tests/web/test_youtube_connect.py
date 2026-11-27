"""Tests for Connect YouTube (publisher client) + quota-project web flow."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils import youtube_upload
from video_grouper.utils.config import TTTConfig
from video_grouper.utils.youtube_quota_project import write_quota_project
from video_grouper.web import youtube_connect
from video_grouper.web.auth_server import (
    _render_youtube_section,
    _success_page,
    create_app,
)
from video_grouper.web.setup.router import _carry_ttt_tokens

PUBLISHER_ID = "publisher.apps.googleusercontent.com"
_SAME_ORIGIN = {"origin": "http://localhost:8765"}


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


@pytest.fixture(autouse=True)
def _reset():
    youtube_connect.reset_state()
    yield
    youtube_connect.reset_state()


@pytest.fixture
def publisher(monkeypatch):
    installed = youtube_upload.EMBEDDED_CLIENT_CONFIG["installed"]
    monkeypatch.setitem(installed, "client_id", PUBLISHER_ID)
    monkeypatch.setitem(installed, "client_secret", "publisher-secret")


@pytest.fixture
def no_publisher(monkeypatch):
    installed = youtube_upload.EMBEDDED_CLIENT_CONFIG["installed"]
    monkeypatch.setitem(installed, "client_id", "")
    monkeypatch.setitem(installed, "client_secret", "")


@pytest.fixture
def client(tmp_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=tmp_path / "config.ini")
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


def _write_publisher_token(storage, client_id=PUBLISHER_ID):
    yt = storage / "youtube"
    yt.mkdir(parents=True, exist_ok=True)
    (yt / "token.json").write_text(
        json.dumps(
            {
                "token": "at",
                "refresh_token": "rt",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": client_id,
                "client_secret": "s",
                "scopes": youtube_upload.PUBLISHER_SCOPES,
                "expiry": "2999-01-01T00:00:00Z",
            }
        )
    )


class _FakeFlow:
    """Stands in for InstalledAppFlow on the callback leg."""

    def __init__(self, email):
        self.credentials = type(
            "C",
            (),
            {
                "id_token": _jwt({"email": email}),
                "to_json": lambda self: json.dumps(
                    {"client_id": PUBLISHER_ID, "refresh_token": "rt"}
                ),
            },
        )()

    def fetch_token(self, code):
        assert code == "the-code"


# ---------------------------------------------------------------- availability


def test_connect_without_publisher_client_explains(client, no_publisher):
    resp = client.get("/auth/youtube/connect", follow_redirects=False)
    assert resp.status_code == 404
    assert "bring-your-own" in resp.text


def test_dashboard_without_publisher_shows_only_byo(tmp_path, no_publisher):
    html = _render_youtube_section(tmp_path)
    assert "/auth/youtube/connect" not in html
    assert "client_secret.json" in html


# ---------------------------------------------------------------- consent 1


def test_connect_redirects_with_account_chooser(client, publisher):
    resp = client.get(
        "/auth/youtube/connect?return_to=/setup/youtube", follow_redirects=False
    )
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["client_id"] == [PUBLISHER_ID]
    assert q["prompt"] == ["select_account consent"]
    assert q["access_type"] == ["offline"]
    assert "youtube.upload" in q["scope"][0] and "email" in q["scope"][0]
    assert q["redirect_uri"] == ["http://localhost:8765/auth/youtube/connect/callback"]


def test_connect_callback_saves_token_then_asks_for_quota(
    client, publisher, tmp_path, monkeypatch
):
    resp = client.get(
        "/auth/youtube/connect?return_to=/setup/youtube", follow_redirects=False
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        youtube_upload, "make_publisher_flow", lambda *a, **k: _FakeFlow("a@x.com")
    )
    resp = client.get(
        f"/auth/youtube/connect/callback?code=the-code&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/youtube/quota?return_to=/setup/youtube"
    assert (tmp_path / "youtube" / "token.json").exists()


def test_reconnecting_same_account_keeps_project(
    client, publisher, tmp_path, monkeypatch
):
    write_quota_project(tmp_path / "youtube", "soccer-cam-keep0001", "a@x.com")
    resp = client.get("/auth/youtube/connect", follow_redirects=False)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        youtube_upload, "make_publisher_flow", lambda *a, **k: _FakeFlow("a@x.com")
    )
    resp = client.get(
        f"/auth/youtube/connect/callback?code=the-code&state={state}",
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/"
    assert (tmp_path / "youtube" / "quota_project.json").exists()


def test_connecting_different_account_drops_old_project(
    client, publisher, tmp_path, monkeypatch
):
    write_quota_project(tmp_path / "youtube", "soccer-cam-old00001", "a@x.com")
    resp = client.get("/auth/youtube/connect", follow_redirects=False)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        youtube_upload, "make_publisher_flow", lambda *a, **k: _FakeFlow("b@x.com")
    )
    resp = client.get(
        f"/auth/youtube/connect/callback?code=the-code&state={state}",
        follow_redirects=False,
    )
    assert resp.headers["location"].startswith("/auth/youtube/quota")
    assert not (tmp_path / "youtube" / "quota_project.json").exists()


def test_connect_callback_rejects_unknown_state(client, publisher):
    resp = client.get("/auth/youtube/connect/callback?code=c&state=forged")
    assert resp.status_code == 400


def test_return_to_cannot_leave_the_server(client, publisher):
    resp = client.get(
        "/auth/youtube/connect?return_to=//evil.example", follow_redirects=False
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    assert youtube_connect._states[state]["return_to"] == "/"


# ---------------------------------------------------------------- consent 2


def test_quota_start_is_online_only_with_login_hint(
    client, publisher, tmp_path, monkeypatch
):
    _write_publisher_token(tmp_path)
    monkeypatch.setattr(youtube_connect, "_account_email", lambda creds: "a@x.com")
    resp = client.get("/auth/youtube/quota/start", follow_redirects=False)
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["access_type"] == ["online"]  # no refresh token for Cloud scopes
    assert q["login_hint"] == ["a@x.com"]
    assert "cloudplatformprojects" in q["scope"][0]
    assert "service.management" in q["scope"][0]
    assert "cloud-platform" not in q["scope"][0]


def test_quota_start_without_youtube_goes_to_connect(client, publisher):
    resp = client.get("/auth/youtube/quota/start", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/youtube/connect")


def test_quota_callback_refuses_a_different_account(
    client, publisher, tmp_path, monkeypatch
):
    _write_publisher_token(tmp_path)
    monkeypatch.setattr(youtube_connect, "_account_email", lambda creds: "a@x.com")
    resp = client.get("/auth/youtube/quota/start", follow_redirects=False)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        youtube_upload, "make_publisher_flow", lambda *a, **k: _FakeFlow("b@x.com")
    )
    resp = client.get(f"/auth/youtube/quota/callback?code=the-code&state={state}")
    assert resp.status_code == 400
    assert "different Google account" in resp.text
    assert youtube_connect._job["provisioner"] is None


def test_quota_callback_starts_provisioner(client, publisher, tmp_path, monkeypatch):
    _write_publisher_token(tmp_path)
    monkeypatch.setattr(youtube_connect, "_account_email", lambda creds: "a@x.com")
    resp = client.get("/auth/youtube/quota/start", follow_redirects=False)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        youtube_upload, "make_publisher_flow", lambda *a, **k: _FakeFlow("a@x.com")
    )
    started = []

    class _Prov:
        def __init__(self, setup, yt, on_ready):
            self.on_ready = on_ready
            self.status = {"phase": "creating", "done": False, "message": "m"}

        def start(self):
            started.append(self)
            self.on_ready("soccer-cam-new00001", True)
            self.status = {"phase": "ready", "done": True, "message": "ok"}

    monkeypatch.setattr(
        "video_grouper.utils.youtube_quota_project.QuotaProjectProvisioner", _Prov
    )
    resp = client.get(
        f"/auth/youtube/quota/callback?code=the-code&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 303 and started
    saved = json.loads((tmp_path / "youtube" / "quota_project.json").read_text())
    assert saved == {**saved, "project_id": "soccer-cam-new00001", "account": "a@x.com"}
    assert client.get("/auth/youtube/quota/status").json()["phase"] == "ready"


# ---------------------------------------------------------------- pages


def test_dashboard_offers_connect_with_byo_behind_disclosure(tmp_path, publisher):
    html = _render_youtube_section(tmp_path)
    assert 'href="/auth/youtube/connect"' in html
    assert "Use my own Google Cloud client" in html


def test_dashboard_flags_missing_quota_project(tmp_path, publisher):
    _write_publisher_token(tmp_path)
    html = _render_youtube_section(tmp_path)
    assert "uploads are paused" in html
    assert "/auth/youtube/quota" in html


def test_dashboard_shows_quota_project(tmp_path, publisher):
    _write_publisher_token(tmp_path)
    write_quota_project(tmp_path / "youtube", "soccer-cam-abc12345", "a@x.com")
    html = _render_youtube_section(tmp_path)
    assert "soccer-cam-abc12345" in html and "a@x.com" in html


def test_ttt_success_offers_youtube_button_not_redirect(tmp_path, publisher):
    page = _success_page(tmp_path, None)
    assert "/auth/youtube/connect" in page
    assert "location.href" not in page  # a choice, never automatic


def test_ttt_success_from_wizard_returns_there(tmp_path, publisher):
    page = _success_page(tmp_path, "/setup/ttt")
    assert 'location.href="/setup/ttt"' in page


def test_quota_page_explains_before_asking(client, publisher, tmp_path):
    _write_publisher_token(tmp_path)
    resp = client.get("/auth/youtube/quota")
    assert "Set up YouTube quota" in resp.text
    assert "never saves it" in resp.text


# ---------------------------------------------------------------- wizard


def test_wizard_youtube_step_leads_with_connect(client, publisher, tmp_path):
    client.get("/setup/welcome")
    client.post("/setup/storage", data={"storage_path": str(tmp_path)})
    resp = client.get("/setup/youtube")
    assert "/auth/youtube/connect?return_to=/setup/youtube" in resp.text
    assert "Use my own Google Cloud client" in resp.text


def test_wizard_ttt_step_offers_sign_in_with_return(client):
    resp = client.get("/setup/ttt")
    assert 'href="/login?return_to=/setup/ttt"' in resp.text


def test_carry_ttt_tokens(tmp_path):
    svc, wiz = tmp_path / "svc", tmp_path / "wiz"
    (svc / "ttt").mkdir(parents=True)
    (svc / "ttt" / "tokens.json").write_text("{}")
    _carry_ttt_tokens(str(svc), str(wiz))
    assert (wiz / "ttt" / "tokens.json").read_text() == "{}"
    # Never overwrites an existing sign-in.
    (svc / "ttt" / "tokens.json").write_text('{"new": 1}')
    _carry_ttt_tokens(str(svc), str(wiz))
    assert (wiz / "ttt" / "tokens.json").read_text() == "{}"


def test_no_channel_is_surfaced_not_hidden(tmp_path, publisher):
    """U8: quota works but uploads would fail -- say so up front."""
    _write_publisher_token(tmp_path)
    write_quota_project(tmp_path / "youtube", "soccer-cam-abc12345", "a@x.com", False)
    assert "youtube.com/create_channel" in _render_youtube_section(tmp_path)
    write_quota_project(tmp_path / "youtube", "soccer-cam-abc12345", "a@x.com", True)
    assert "create_channel" not in _render_youtube_section(tmp_path)
