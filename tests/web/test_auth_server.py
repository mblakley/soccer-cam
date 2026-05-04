"""Tests for the headless TTT auth web server (Supabase OAuth + dashboard)."""

import base64
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from video_grouper.api_integrations.ttt_api import TTTApiClient, TTTApiError
from video_grouper.utils.config import TTTConfig
from video_grouper.web import auth_server
from video_grouper.web.auth_server import create_app


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _make_jwt(
    *, sub: str = "user-123", exp: float | int, email: str | None = None
) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    payload = {"sub": sub, "exp": exp}
    if email:
        payload["email"] = email
    body = _b64url(json.dumps(payload).encode("utf-8"))
    sig = _b64url(b"signature")
    return f"{header}.{body}.{sig}"


def _ttt_config(**overrides) -> TTTConfig:
    return TTTConfig(**overrides)


def _write_tokens(storage, *, jwt: str, refresh: str = "r", expires_at: float):
    ttt_dir = storage / "ttt"
    ttt_dir.mkdir(parents=True, exist_ok=True)
    (ttt_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": jwt,
                "refresh_token": refresh,
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )


def _write_game(
    storage, name: str, status: str, files: int = 0, error: str | None = None
):
    game_dir = storage / name
    game_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "files": {f"file{i}.dav": {} for i in range(files)},
    }
    if error:
        payload["error_message"] = error
    (game_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def storage(tmp_path):
    return tmp_path


@pytest.fixture
def client(storage):
    app = create_app(_ttt_config(), str(storage))
    # Use a loopback base_url so the auth server's Host-allowlist middleware
    # accepts the requests; the default `http://testserver` would 403.
    with TestClient(app, base_url="http://localhost:8765") as c:
        yield c


# Same-origin headers for state-changing POSTs (matches the test client's
# base_url). The middleware now requires Origin or Referer to be present.
_SAME_ORIGIN = {"origin": "http://localhost:8765"}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_unauthenticated_shows_all_signin_methods(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Soccer-Cam" in body
    assert "Not signed in" in body

    # OAuth provider buttons for all enabled providers.
    for provider in ("google", "discord", "apple", "facebook", "twitter"):
        assert f'href="/login?provider={provider}"' in body, f"missing {provider}"

    # Email/password form.
    assert 'action="/login/password"' in body
    assert 'name="password"' in body

    # Magic link form.
    assert 'action="/login/magic"' in body


def test_dashboard_authenticated_shows_email_when_present(storage, client):
    exp = int(time.time()) + 3600
    _write_tokens(
        storage,
        jwt=_make_jwt(sub="user-abc", exp=exp, email="mark@example.com"),
        expires_at=float(exp),
    )
    body = client.get("/").text
    assert "Signed in as" in body
    # Prefers email over the raw sub UUID.
    assert "mark@example.com" in body
    # Sign-out is a POST form, not a GET link.
    assert 'action="/logout"' in body
    assert 'method="post"' in body
    # Sign-in widgets should be gone.
    assert 'action="/login/password"' not in body
    assert 'action="/login/magic"' not in body


def test_dashboard_authenticated_falls_back_to_sub_when_no_email(storage, client):
    exp = int(time.time()) + 3600
    _write_tokens(
        storage, jwt=_make_jwt(sub="user-abc", exp=exp), expires_at=float(exp)
    )
    body = client.get("/").text
    assert "Signed in as" in body
    assert "user-abc" in body


def test_dashboard_expired_token_surfaces_warning(storage, client):
    past = int(time.time()) - 60
    _write_tokens(
        storage, jwt=_make_jwt(sub="user-xyz", exp=past), expires_at=float(past)
    )
    body = client.get("/").text
    assert "Token expired" in body
    # Re-sign-in still offered (sign-in widgets present).
    assert 'action="/login/password"' in body


def test_dashboard_includes_pipeline_status_from_provider(storage):
    def provider():
        return {
            "queue_sizes": {"download": 2, "video": 0, "upload": 1},
            "cameras": [
                {"name": "default", "ip": "192.168.1.100", "connected": True},
                {"name": "backyard", "ip": "10.0.0.5", "connected": False},
            ],
        }

    app = create_app(_ttt_config(), str(storage), status_provider=provider)
    with TestClient(app, base_url="http://localhost:8765") as c:
        body = c.get("/").text

    # Queues
    assert "download" in body and ">2<" in body
    assert "video" in body and ">0<" in body
    # Cameras
    assert "default" in body and "192.168.1.100" in body
    assert "connected" in body
    assert "backyard" in body and "not connected" in body


def test_dashboard_pipeline_section_when_no_status_provider(client):
    resp = client.get("/")
    assert "No live pipeline status available" in resp.text
    assert "No cameras configured" in resp.text


def test_dashboard_status_provider_exception_falls_back_silently(storage):
    def boom():
        raise RuntimeError("orchestrator down")

    app = create_app(_ttt_config(), str(storage), status_provider=boom)
    with TestClient(app, base_url="http://localhost:8765") as c:
        resp = c.get("/")
    assert resp.status_code == 200
    # Falls back to "no live status" rather than 500ing.
    assert "No live pipeline status available" in resp.text


def test_dashboard_lists_games_from_storage(storage, client):
    _write_game(storage, "2026.04.20-14.30.00", "downloaded", files=5)
    _write_game(
        storage, "2026.04.21-15.00.00", "trimmed", files=3, error="ffmpeg blew up"
    )
    # Non-game dir + ttt dir should be ignored.
    (storage / "logs").mkdir()
    (storage / "ttt").mkdir()

    body = client.get("/").text
    assert "2026.04.20-14.30.00" in body
    assert "downloaded" in body
    assert "5 files" in body
    assert "2026.04.21-15.00.00" in body
    assert "trimmed" in body
    assert "ffmpeg blew up" in body
    # logs/ shouldn't appear as a game.
    assert ">logs<" not in body


def test_dashboard_no_games_message(client):
    body = client.get("/").text
    assert "No game groups in storage yet" in body


def test_dashboard_auto_refreshes(client):
    body = client.get("/").text
    assert '<meta http-equiv="refresh"' in body


# ---------------------------------------------------------------------------
# Dashboard redirect to /setup/welcome when onboarding isn't done
# (Phase 2 done-criterion: a fresh shared_data with no config.ini boots
# the service; the dashboard sends the user to the wizard.)
# ---------------------------------------------------------------------------


def test_dashboard_redirects_to_setup_when_config_missing(tmp_path):
    """When config_path is wired but the file doesn't exist, '/' bounces
    to /setup/welcome instead of rendering the dashboard."""
    config_path = tmp_path / "config.ini"  # deliberately not created
    app = create_app(_ttt_config(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765") as c:
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/welcome"


def test_dashboard_redirects_to_setup_when_onboarding_incomplete(tmp_path):
    """A config that exists but with onboarding_completed=False also
    bounces — covers the 'wizard partway through' case."""
    from video_grouper.utils.config import create_default_config

    config_path = tmp_path / "config.ini"
    create_default_config(
        config_path, str(tmp_path)
    )  # writes onboarding_completed=False
    app = create_app(_ttt_config(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765") as c:
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/welcome"


def test_dashboard_renders_when_onboarding_completed(tmp_path):
    """Once the user finishes the wizard, the dashboard is the default
    landing page again."""
    from video_grouper.utils.config import (
        create_default_config,
        load_config,
        save_config,
    )

    config_path = tmp_path / "config.ini"
    create_default_config(config_path, str(tmp_path))
    cfg = load_config(config_path)
    cfg.setup.onboarding_completed = True
    save_config(cfg, config_path)

    app = create_app(_ttt_config(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765") as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert "Soccer-Cam" in resp.text


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def test_login_redirects_to_supabase_authorize(client):
    resp = client.get(
        "/login?provider=google",
        follow_redirects=False,
        headers={"host": "localhost:8765"},
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlparse(location)

    assert location.startswith(auth_server.TTT_SUPABASE_URL)
    assert parsed.path == "/auth/v1/authorize"
    qs = parse_qs(parsed.query)
    assert qs["provider"] == ["google"]
    assert qs["redirect_to"] == ["http://localhost:8765/callback"]
    # GoTrue manages OAuth state itself; our /login must NOT inject one
    # or GoTrue's /callback rejects with "OAuth state parameter is invalid".
    assert "state" not in qs
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


def test_login_uses_request_host_for_redirect_uri(storage):
    """User signing in via a non-localhost name needs that name on the
    Host allowlist (configured via auth_server_bind) AND in the redirect."""
    cfg = _ttt_config(auth_server_bind="nas.local")
    app = create_app(cfg, str(storage))
    with TestClient(app, base_url="http://nas.local:8765") as c:
        resp = c.get("/login?provider=google", follow_redirects=False)
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["redirect_to"] == ["http://nas.local:8765/callback"]


def test_login_respects_x_forwarded_proto(storage):
    cfg = _ttt_config(auth_server_bind="auth.example.com")
    app = create_app(cfg, str(storage))
    with TestClient(app, base_url="http://auth.example.com") as c:
        resp = c.get(
            "/login?provider=google",
            follow_redirects=False,
            headers={"x-forwarded-proto": "https"},
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["redirect_to"] == ["https://auth.example.com/callback"]


def test_callback_serves_fragment_extraction_page(client):
    resp = client.get("/callback")
    assert resp.status_code == 200
    body = resp.text
    # Reads the OAuth response from the URL fragment.
    assert "window.location.hash" in body
    # POSTs (not GETs) to /receive-token so the same-origin middleware
    # applies; an <img>/<script> attack can only do GET.
    assert "/receive-token" in body
    assert "method: 'POST'" in body
    assert "access_token" in body
    # Should no longer build a GET URL with query-string params.
    assert "window.location.replace" not in body


def test_receive_token_persists_tokens_and_returns_success(storage, client):
    exp = int(time.time()) + 3600
    jwt = _make_jwt(sub="user-abc", exp=exp)

    resp = client.post(
        "/receive-token",
        json={"access_token": jwt},
        headers=_SAME_ORIGIN,
    )
    assert resp.status_code == 200
    assert "Signed in" in resp.text

    token_file = storage / "ttt" / "tokens.json"
    assert token_file.exists()
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == jwt
    assert saved["refresh_token"] is None
    assert int(float(saved["expires_at"])) == exp


def test_receive_token_with_error_returns_400_page(client):
    resp = client.post(
        "/receive-token",
        json={"error": "access_denied", "error_description": "User canceled"},
        headers=_SAME_ORIGIN,
    )
    assert resp.status_code == 400
    assert "User canceled" in resp.text


def test_receive_token_missing_token_returns_400(client):
    resp = client.post(
        "/receive-token",
        json={},
        headers=_SAME_ORIGIN,
    )
    assert resp.status_code == 400
    assert "No access token returned" in resp.text


def test_receive_token_get_returns_405(client):
    """Replaces the old state-cookie defense: <img src=/receive-token?...>
    can't drive sign-in because GET is no longer accepted."""
    resp = client.get("/receive-token?access_token=fake")
    assert resp.status_code == 405


def test_receive_token_post_rejects_no_origin_or_referer(client):
    """Curl-style POST with no Origin or Referer is now rejected; the
    middleware fails closed for state-changing methods."""
    resp = client.post("/receive-token", json={"access_token": "fake"})
    assert resp.status_code == 403


def test_receive_token_post_rejects_cross_origin(client):
    resp = client.post(
        "/receive-token",
        json={"access_token": "fake"},
        headers={"origin": "https://evil.com"},
    )
    assert resp.status_code == 403


def test_receive_token_post_accepts_same_origin_referer(storage, client):
    """Referer-only is sufficient — some browsers strip Origin on
    same-origin navigations and we accept either signal."""
    exp = int(time.time()) + 3600
    jwt = _make_jwt(sub="user-ref", exp=exp)
    resp = client.post(
        "/receive-token",
        json={"access_token": jwt},
        headers={"referer": "http://localhost:8765/callback"},
    )
    assert resp.status_code == 200
    assert (storage / "ttt" / "tokens.json").exists()


# ---------------------------------------------------------------------------
# Local-web-app hardening: DNS rebinding + Origin/Referer
# ---------------------------------------------------------------------------


def test_host_header_rejected_for_unknown_host(client):
    """DNS rebinding defense: an attacker-resolved name that points at
    127.0.0.1 should not be able to drive our endpoints."""
    resp = client.get("/", headers={"host": "evil.com"})
    assert resp.status_code == 403


def test_host_header_accepted_for_loopback_variants(storage):
    app = create_app(_ttt_config(), str(storage))
    for host in ("localhost", "127.0.0.1"):
        with TestClient(app, base_url=f"http://{host}") as c:
            resp = c.get("/")
            assert resp.status_code == 200, host


def test_post_logout_rejects_cross_origin(client, storage):
    """Cross-origin POST from a malicious page is blocked."""
    # Set up a token so the test isn't testing the no-op path.
    _write_tokens(
        storage,
        jwt=_make_jwt(exp=int(time.time()) + 60),
        expires_at=float(time.time() + 60),
    )
    resp = client.post(
        "/logout",
        headers={"origin": "https://evil.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    # Token should NOT have been deleted.
    assert (storage / "ttt" / "tokens.json").exists()


def test_post_logout_accepts_same_origin(client, storage):
    _write_tokens(
        storage,
        jwt=_make_jwt(exp=int(time.time()) + 60),
        expires_at=float(time.time() + 60),
    )
    resp = client.post(
        "/logout",
        headers={"origin": "http://localhost:8765"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not (storage / "ttt" / "tokens.json").exists()


def test_post_login_password_rejects_cross_origin(client):
    resp = client.post(
        "/login/password",
        headers={"origin": "https://evil.com"},
        data={"email": "x@y.com", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Phase 0b: auth-needed banner + flag helpers
# ---------------------------------------------------------------------------


def test_dashboard_no_banner_when_no_flag(client):
    body = client.get("/").text
    assert 'class="banner"' not in body


def test_dashboard_renders_banner_when_youtube_flag_present(storage, client):
    from video_grouper.web.auth_status import write_auth_needed

    write_auth_needed(
        storage, "youtube", "invalid_grant: Token has been expired or revoked."
    )
    body = client.get("/").text
    assert 'class="banner"' in body
    assert "youtube" in body
    assert "invalid_grant" in body


def test_dashboard_banner_clears_when_flag_removed(storage, client):
    from video_grouper.web.auth_status import (
        clear_auth_needed,
        write_auth_needed,
    )

    write_auth_needed(storage, "youtube", "boom")
    assert 'class="banner"' in client.get("/").text
    clear_auth_needed(storage, "youtube")
    assert 'class="banner"' not in client.get("/").text


def test_is_hard_youtube_auth_failure_classifier():
    from video_grouper.web.auth_status import is_hard_youtube_auth_failure

    # Hard cases — refresh token broken, can't recover headlessly.
    assert is_hard_youtube_auth_failure(
        Exception("invalid_grant: Token has been expired or revoked.")
    )
    assert is_hard_youtube_auth_failure(Exception("unauthorized_client"))
    assert is_hard_youtube_auth_failure(Exception("No refresh token; reauth"))

    # Transient — should NOT be flagged as hard.
    assert not is_hard_youtube_auth_failure(Exception("Connection reset by peer"))
    assert not is_hard_youtube_auth_failure(Exception("HTTP 503 Service Unavailable"))
    assert not is_hard_youtube_auth_failure(Exception("rateLimitExceeded"))


def test_list_auth_needed_collects_multiple_flags(storage):
    from video_grouper.web.auth_status import (
        list_auth_needed,
        write_auth_needed,
    )

    write_auth_needed(storage, "youtube", "y err")
    write_auth_needed(storage, "ttt", "t err")

    flags = list_auth_needed(storage)
    providers = {f["provider"] for f in flags}
    assert providers == {"youtube", "ttt"}


def test_login_password_success_persists_tokens_and_redirects(storage, client):
    exp = int(time.time()) + 3600
    jwt = _make_jwt(sub="user-pw", exp=exp, email="pw@example.com")

    def fake_login(self, email, password):
        assert email == "pw@example.com"
        assert password == "hunter2"
        self._access_token = jwt
        self._refresh_token_value = "refresh-pw"
        self._expires_at = float(exp)
        self._save_tokens()

    with patch.object(TTTApiClient, "login", autospec=True, side_effect=fake_login):
        resp = client.post(
            "/login/password",
            data={"email": "pw@example.com", "password": "hunter2"},
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    saved = json.loads((storage / "ttt" / "tokens.json").read_text(encoding="utf-8"))
    assert saved["access_token"] == jwt


def test_login_password_failure_returns_401(client):
    with patch.object(
        TTTApiClient,
        "login",
        autospec=True,
        side_effect=TTTApiError("Invalid login credentials", status_code=400),
    ):
        resp = client.post(
            "/login/password",
            data={"email": "x@y.com", "password": "wrong"},
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )
    assert resp.status_code == 401
    assert "Invalid login credentials" in resp.text


def test_login_magic_calls_supabase_with_correct_redirect_to(client):
    captured = {}

    def fake_send(self, email, redirect_to):
        captured["email"] = email
        captured["redirect_to"] = redirect_to

    with patch.object(
        TTTApiClient, "send_magic_link", autospec=True, side_effect=fake_send
    ):
        resp = client.post(
            "/login/magic",
            data={"email": "user@example.com"},
            headers={"host": "localhost:8765", **_SAME_ORIGIN},
            follow_redirects=False,
        )

    assert resp.status_code == 200
    assert "Magic link sent" in resp.text
    assert "user@example.com" in resp.text
    assert captured["email"] == "user@example.com"
    assert captured["redirect_to"] == "http://localhost:8765/callback"


def test_login_magic_failure_returns_400(client):
    with patch.object(
        TTTApiClient,
        "send_magic_link",
        autospec=True,
        side_effect=TTTApiError("rate limited", status_code=429),
    ):
        resp = client.post(
            "/login/magic",
            data={"email": "x@y.com"},
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "rate limited" in resp.text


def test_logout_redirects_to_dashboard_and_is_idempotent(storage, client):
    exp = int(time.time()) + 60
    _write_tokens(storage, jwt=_make_jwt(exp=exp), expires_at=float(exp))
    token_file = storage / "ttt" / "tokens.json"

    resp = client.post("/logout", headers=_SAME_ORIGIN, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert not token_file.exists()

    # Second call: still redirects, no error.
    resp2 = client.post("/logout", headers=_SAME_ORIGIN, follow_redirects=False)
    assert resp2.status_code == 303


# ---------------------------------------------------------------------------
# URL resolution (single field + split internal/external)
# ---------------------------------------------------------------------------


def test_url_resolution_uses_build_time_defaults_when_blank(storage):
    captured = {}
    real_init = TTTApiClient.__init__

    def capture_init(self, supabase_url, anon_key, api_base_url, storage_path):
        captured["supabase_url"] = supabase_url
        captured["anon_key"] = anon_key
        captured["api_base_url"] = api_base_url
        real_init(self, supabase_url, anon_key, api_base_url, storage_path)

    with patch.object(TTTApiClient, "__init__", capture_init):
        create_app(_ttt_config(), str(storage))

    assert captured["supabase_url"] == auth_server.TTT_SUPABASE_URL
    assert captured["anon_key"] == auth_server.TTT_ANON_KEY
    assert captured["api_base_url"] == auth_server.TTT_API_BASE_URL


def test_url_resolution_dev_override_wins(storage):
    captured = {}
    real_init = TTTApiClient.__init__

    def capture_init(self, supabase_url, anon_key, api_base_url, storage_path):
        captured["supabase_url"] = supabase_url
        captured["anon_key"] = anon_key
        captured["api_base_url"] = api_base_url
        real_init(self, supabase_url, anon_key, api_base_url, storage_path)

    cfg = _ttt_config(
        supabase_url="http://override.local",
        anon_key="override-key",
        api_base_url="http://override.api",
    )
    with patch.object(TTTApiClient, "__init__", capture_init):
        create_app(cfg, str(storage))

    assert captured["supabase_url"] == "http://override.local"
    assert captured["anon_key"] == "override-key"
    assert captured["api_base_url"] == "http://override.api"


def test_supabase_internal_url_used_for_client_external_used_for_redirect(storage):
    """When [TTT].supabase_internal_url is set, the TTTApiClient uses it for
    outbound HTTP, while the /login redirect keeps emitting [TTT].supabase_url."""
    captured = {}
    real_init = TTTApiClient.__init__

    def capture_init(self, supabase_url, anon_key, api_base_url, storage_path):
        captured["supabase_url"] = supabase_url
        real_init(self, supabase_url, anon_key, api_base_url, storage_path)

    cfg = _ttt_config(
        supabase_url="http://localhost:54321",
        supabase_internal_url="http://supabase_kong_local:8000",
        anon_key="k",
        api_base_url="http://api.local",
    )
    with patch.object(TTTApiClient, "__init__", capture_init):
        app = create_app(cfg, str(storage))

    assert captured["supabase_url"] == "http://supabase_kong_local:8000"

    with TestClient(app) as c:
        resp = c.get(
            "/login?provider=google",
            follow_redirects=False,
            headers={"host": "localhost:8765"},
        )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(
        "http://localhost:54321/auth/v1/authorize"
    )
    assert "supabase_kong_local" not in resp.headers["location"]


def test_supabase_internal_url_blank_falls_back_to_external(storage):
    captured = {}
    real_init = TTTApiClient.__init__

    def capture_init(self, supabase_url, anon_key, api_base_url, storage_path):
        captured["supabase_url"] = supabase_url
        real_init(self, supabase_url, anon_key, api_base_url, storage_path)

    cfg = _ttt_config(supabase_url="http://prod.example.com")
    with patch.object(TTTApiClient, "__init__", capture_init):
        create_app(cfg, str(storage))

    assert captured["supabase_url"] == "http://prod.example.com"
