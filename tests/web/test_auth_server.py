"""Tests for the headless TTT auth web server (Supabase OAuth + dashboard)."""

import base64
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from video_grouper.api_integrations.ttt_api import TTTApiError
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
    assert "Session expired" in body
    # Re-sign-in still offered.
    assert 'href="/login"' in body


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
    # Cameras. Status labels are sentence case, matching the rest of the UI.
    assert "default" in body and "192.168.1.100" in body
    assert ">Connected<" in body or "Connected" in body
    assert "backyard" in body and "Not connected" in body


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
# Per-recording reprocess UI + endpoint
# ---------------------------------------------------------------------------


class TestReprocessEndpoint:
    """The dashboard exposes a per-game reprocess form that writes
    ``reprocess_request.json`` for the runner to pick up. Tests cover the
    POST endpoint contract; the spec-list patching is unit-tested in
    ``test_pipeline_reprocess.py``."""

    GAME = "2026.05.20-14.30.00"

    def test_each_game_row_has_a_reprocess_form(self, storage, client):
        _write_game(storage, self.GAME, "pipeline_complete", files=3)
        body = client.get("/").text
        assert f'action="/recordings/{self.GAME}/reprocess"' in body
        assert "stabilization_strength" in body
        for s in ("light", "standard", "heavy", "extreme"):
            assert f'value="{s}"' in body
        assert "skip detect" in body.lower()

    def test_post_writes_reprocess_request_json(self, storage, client):
        _write_game(storage, self.GAME, "pipeline_complete", files=3)
        r = client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "extreme", "skip_detect": "1"},
            headers={"Origin": "http://localhost:8765"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        out = json.loads(
            (storage / self.GAME / "reprocess_request.json").read_text(encoding="utf-8")
        )
        assert out["stabilization_strength"] == "extreme"
        assert out["skip_detect"] is True
        assert out["requested_by"] == "tray"
        assert out["requested_at"]  # ISO 8601 string

    def test_post_without_skip_detect_falls_back_to_false(self, storage, client):
        _write_game(storage, self.GAME, "complete")
        client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "light"},  # no skip_detect field
            headers={"Origin": "http://localhost:8765"},
            follow_redirects=False,
        )
        out = json.loads(
            (storage / self.GAME / "reprocess_request.json").read_text(encoding="utf-8")
        )
        assert out["skip_detect"] is False

    def test_post_rolls_terminal_state_back_to_queued(self, storage, client):
        """A completed game must be re-queued for the pipeline_processor
        to pick up; without this nudge the discovery loop would skip it."""
        _write_game(storage, self.GAME, "pipeline_complete")
        client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "heavy"},
            headers={"Origin": "http://localhost:8765"},
            follow_redirects=False,
        )
        state = json.loads(
            (storage / self.GAME / "state.json").read_text(encoding="utf-8")
        )
        assert state["status"] == "pipeline_queued_reprocess"

    def test_post_leaves_in_progress_state_alone(self, storage, client):
        """Don't clobber state while the pipeline is mid-run."""
        _write_game(storage, self.GAME, "downloading")
        client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "heavy"},
            headers={"Origin": "http://localhost:8765"},
            follow_redirects=False,
        )
        state = json.loads(
            (storage / self.GAME / "state.json").read_text(encoding="utf-8")
        )
        assert state["status"] == "downloading"

    def test_post_rejects_invalid_strength(self, storage, client):
        _write_game(storage, self.GAME, "complete")
        r = client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "ridiculous"},
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code == 400

    def test_post_rejects_path_traversal(self, storage, client):
        r = client.post(
            "/recordings/..%2F..%2Fetc/reprocess",
            data={"stabilization_strength": "heavy"},
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code in (400, 404)

    def test_post_rejects_unknown_recording(self, storage, client):
        r = client.post(
            "/recordings/2099.01.01-00.00.00/reprocess",
            data={"stabilization_strength": "heavy"},
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code == 404

    def _seed_running_state(self, storage):
        """Make is_pipeline_running return True for the test game."""
        (storage / self.GAME / "pipeline_state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "input_path": "",
                    "output_path": "",
                    "artifacts": {},
                    "steps": [
                        {
                            "step_id": "stabilize",
                            "type": "stabilize",
                            "status": "running",
                        }
                    ],
                }
            )
        )

    def test_post_rejected_with_409_when_pipeline_running(self, storage, client):
        """Single in-flight reprocess: a POST while a run is in progress
        must fail rather than overwriting the request file mid-run."""
        _write_game(storage, self.GAME, "running", files=3)
        self._seed_running_state(storage)
        r = client.post(
            f"/recordings/{self.GAME}/reprocess",
            data={"stabilization_strength": "extreme", "skip_detect": "1"},
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code == 409
        assert not (storage / self.GAME / "reprocess_request.json").exists()

    def test_dashboard_shows_cancel_button_when_running(self, storage, client):
        """Form is replaced by a Cancel button while the pipeline is
        running — the user can't change settings without cancelling first."""
        _write_game(storage, self.GAME, "running", files=3)
        self._seed_running_state(storage)
        body = client.get("/").text
        assert f"/recordings/{self.GAME}/cancel" in body
        assert "Cancel" in body
        # The strength <select> should be gone for this row.
        assert 'name="stabilization_strength"' not in body

    def test_cancel_writes_marker(self, storage, client):
        _write_game(storage, self.GAME, "running")
        self._seed_running_state(storage)
        r = client.post(
            f"/recordings/{self.GAME}/cancel",
            headers={"Origin": "http://localhost:8765"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        marker = storage / self.GAME / "cancel_request.json"
        assert marker.exists()
        # Marker carries a timestamp so post-mortem logs can see when it happened.
        body = json.loads(marker.read_text())
        assert "requested_at" in body

    def test_cancel_rejected_when_not_running(self, storage, client):
        """A cancel for a completed group is a user error — return 409
        so the dashboard surfaces it instead of silently doing nothing."""
        _write_game(storage, self.GAME, "pipeline_complete")
        r = client.post(
            f"/recordings/{self.GAME}/cancel",
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code == 409

    def test_cancel_rejects_path_traversal(self, storage, client):
        r = client.post(
            "/recordings/..%2F..%2Fetc/cancel",
            headers={"Origin": "http://localhost:8765"},
        )
        assert r.status_code in (400, 404)

    def test_form_shows_current_strength_when_already_set(self, storage, client):
        _write_game(storage, self.GAME, "pipeline_complete")
        (storage / self.GAME / "reprocess_request.json").write_text(
            json.dumps({"stabilization_strength": "extreme", "skip_detect": True})
        )
        body = client.get("/").text
        # The select preselects 'extreme', and the row labels its current state.
        assert 'value="extreme" selected' in body
        assert "current: extreme" in body


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


# ---------------------------------------------------------------------------
# Audit 2.2: auto-claim camera-manager on sign-in
# ---------------------------------------------------------------------------


def test_auto_claim_skipped_when_not_authenticated(storage):
    """Helper is a no-op if the client somehow has no valid token yet."""
    from video_grouper.web.auth_server import auto_claim_camera_manager

    fake = type(
        "FakeClient",
        (),
        {
            "is_authenticated": lambda self: False,
            "get_team_assignments": lambda self: pytest.fail(
                "get_team_assignments must not be called when not authenticated"
            ),
            "register_as_camera_manager": lambda self: pytest.fail(
                "register_as_camera_manager must not be called when not authenticated"
            ),
        },
    )()
    auto_claim_camera_manager(fake)


def test_auto_claim_calls_register_when_assignments_empty(storage):
    from video_grouper.web.auth_server import auto_claim_camera_manager

    calls: dict[str, int] = {"get": 0, "register": 0}

    class Fake:
        def is_authenticated(self):
            return True

        def get_team_assignments(self):
            calls["get"] += 1
            return []

        def register_as_camera_manager(self):
            calls["register"] += 1
            return [{"id": "cm-1", "team_id": "team-1"}]

    auto_claim_camera_manager(Fake())
    assert calls == {"get": 1, "register": 1}


def test_auto_claim_skips_register_when_assignments_present(storage):
    from video_grouper.web.auth_server import auto_claim_camera_manager

    calls: dict[str, int] = {"get": 0, "register": 0}

    class Fake:
        def is_authenticated(self):
            return True

        def get_team_assignments(self):
            calls["get"] += 1
            return [{"camera_manager_id": "cm-1", "team_id": "team-1"}]

        def register_as_camera_manager(self):
            calls["register"] += 1
            return []

    auto_claim_camera_manager(Fake())
    assert calls == {"get": 1, "register": 0}


def test_auto_claim_swallows_get_failure(storage):
    """Network error on get_team_assignments must not raise — sign-in
    completes regardless. Register is not attempted in this case."""
    from video_grouper.web.auth_server import auto_claim_camera_manager

    calls = {"register": 0}

    class Fake:
        def is_authenticated(self):
            return True

        def get_team_assignments(self):
            raise TTTApiError("network down", status_code=None)

        def register_as_camera_manager(self):
            calls["register"] += 1
            return []

    # Must not raise.
    auto_claim_camera_manager(Fake())
    assert calls["register"] == 0


def test_auto_claim_swallows_register_failure(storage):
    from video_grouper.web.auth_server import auto_claim_camera_manager

    class Fake:
        def is_authenticated(self):
            return True

        def get_team_assignments(self):
            return []

        def register_as_camera_manager(self):
            raise TTTApiError("server down", status_code=500)

    # Must not raise.
    auto_claim_camera_manager(Fake())


def test_auto_claim_swallows_arbitrary_exceptions(storage):
    """Defense-in-depth: even a bug in the API client (e.g. AttributeError)
    must not break sign-in."""
    from video_grouper.web.auth_server import auto_claim_camera_manager

    class Fake:
        def is_authenticated(self):
            return True

        def get_team_assignments(self):
            raise RuntimeError("unexpected client bug")

        def register_as_camera_manager(self):
            raise AssertionError("should not be called")

    auto_claim_camera_manager(Fake())


# ---------------------------------------------------------------------------
# CSRF guard vs. the tray's own POST (regression: 2026-08-03)
# ---------------------------------------------------------------------------


class _StubUpdateProcessor:
    """Minimal stand-in for UpdateCheckProcessor's HTTP surface."""

    storage_path = "."
    _pending_version = "9.9.9"

    def build_status(self):
        from video_grouper.task_processors.update_check_processor import UpdateStatus

        return UpdateStatus(current_version="0.0.1", auto_update=True)

    async def apply_pending(self):
        return True, "spawned"


def test_update_apply_403s_without_origin_but_accepts_the_trays_own(storage):
    """The tray drives Install Update with ``urllib.request``, which
    sends neither Origin nor Referer -- so the rebinding/CSRF guard
    answered every click with 403 and the manual upgrade path was
    dead. The guard must stay on for headerless callers while the
    tray's same-origin POST gets through.

    These tests previously mounted ``build_router`` on a bare FastAPI
    app, which is why the middleware never ran and the bug shipped.
    """
    app = create_app(
        _ttt_config(), str(storage), update_processor=_StubUpdateProcessor()
    )
    with TestClient(app, base_url="http://localhost:8765") as c:
        headerless = c.post("/api/update/apply")
        assert headerless.status_code == 403
        assert "Missing Origin/Referer" in headerless.text

        # Exactly what video_grouper.tray.main.apply_pending_update sends.
        with_origin = c.post("/api/update/apply", headers=_SAME_ORIGIN)
        assert with_origin.status_code == 202
        assert with_origin.json()["status"] == "spawned"


# ---------------------------------------------------------------------------
# TTT sign-in: TTT's device-auth flow (soccer-cam knows only TTT's API URL)
# ---------------------------------------------------------------------------


def _start_login(client, path="/login", **headers):
    resp = client.get(path, follow_redirects=False, headers=headers or None)
    assert resp.status_code == 302
    target = urlparse(resp.headers["location"])
    return target, parse_qs(target.query)


def test_dashboard_unauthenticated_offers_one_ttt_sign_in(client):
    body = client.get("/").text
    assert 'href="/login"' in body
    assert "Sign in to Team Tech Tools" in body
    for gone in ("/login/password", "/login/magic", "supabase"):
        assert gone not in body


def test_login_sends_browser_to_ttt_device_auth_with_pkce(storage):
    app = create_app(_ttt_config(api_base_url="https://ttt.example"), str(storage))
    with TestClient(app, base_url="http://localhost:8765") as c:
        target, q = _start_login(c)
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        "https://ttt.example/api/device-auth/start"
    )
    assert q["redirect_uri"] == ["http://localhost:8765/ttt/callback"]
    assert len(q["code_challenge"][0]) == 43  # S256, unpadded
    assert q["state"][0] in auth_server._ttt_sign_in_states


def test_login_respects_x_forwarded_proto(storage):
    app = create_app(_ttt_config(api_base_url="https://ttt.example"), str(storage))
    with TestClient(app, base_url="http://localhost:8765") as c:
        _, q = _start_login(c, **{"x-forwarded-proto": "https"})
    assert q["redirect_uri"] == ["https://localhost:8765/ttt/callback"]


def test_api_url_resolution(storage, monkeypatch):
    from video_grouper.api_integrations import ttt_api

    assert ttt_api.resolve_api_base_url("https://dev.example/") == "https://dev.example"
    monkeypatch.setitem(
        __import__("sys").modules,
        "video_grouper.utils._ttt_config",
        type("M", (), {"TTT_API_BASE_URL": "https://built.example"}),
    )
    assert ttt_api.resolve_api_base_url("") == "https://built.example"


def _redeem(client, storage, *, auto_claim=None, token_status=200):
    """Run /login then /ttt/callback with TTT's token endpoint stubbed."""
    _, q = _start_login(client)
    state = q["state"][0]
    exp = int(time.time()) + 3600
    tokens = {
        "access_token": _make_jwt(sub="user-dev", exp=exp, email="m@example.com"),
        "refresh_token": "device-refresh",
        "expires_in": 3600,
    }
    seen = {}

    def fake_post(self, url, json=None, **kw):
        seen["url"], seen["json"] = url, json
        body = tokens if token_status == 200 else {"detail": "invalid_grant"}
        return type(
            "R", (), {"status_code": token_status, "json": lambda s: body, "text": ""}
        )()

    with (
        patch("httpx.Client.post", fake_post),
        patch.object(
            auth_server, "auto_claim_camera_manager", side_effect=auto_claim
        ) as claim,
    ):
        resp = client.get(f"/ttt/callback?code=one-time&state={state}")
    return resp, seen, claim


def test_callback_redeems_code_and_stores_refreshable_session(storage, client):
    resp, seen, claim = _redeem(client, storage)
    assert resp.status_code == 200 and "Signed in" in resp.text
    assert seen["url"].endswith("/api/device-auth/token")
    assert seen["json"]["code"] == "one-time"
    assert seen["json"]["redirect_uri"] == "http://localhost:8765/ttt/callback"
    assert len(seen["json"]["code_verifier"]) >= 43
    stored = json.loads((storage / "ttt" / "tokens.json").read_text())
    # The old OAuth path dropped this, so sessions died after an hour.
    assert stored["refresh_token"] == "device-refresh"
    claim.assert_called_once()


def test_callback_state_is_single_use(storage, client):
    _, q = _start_login(client)
    state = q["state"][0]
    auth_server._ttt_sign_in_states.pop(state)
    resp = client.get(f"/ttt/callback?code=x&state={state}")
    assert resp.status_code == 400


def test_callback_rejects_forged_state(client):
    resp = client.get("/ttt/callback?code=x&state=forged")
    assert resp.status_code == 400


def test_callback_cancelled_by_user(client):
    _, q = _start_login(client)
    resp = client.get(f"/ttt/callback?error=access_denied&state={q['state'][0]}")
    assert resp.status_code == 400
    assert "cancelled" in resp.text


def test_callback_surfaces_rejected_code(storage, client):
    resp, _, claim = _redeem(client, storage, token_status=403)
    assert resp.status_code == 400
    assert not (storage / "ttt" / "tokens.json").exists()
    claim.assert_not_called()


def test_login_from_wizard_returns_there(storage, client):
    _, q = _start_login(client, path="/login?return_to=/setup/ttt")
    assert auth_server._ttt_sign_in_states[q["state"][0]]["return_to"] == "/setup/ttt"
