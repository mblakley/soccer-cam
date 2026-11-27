"""Tests for the user-owned YouTube quota project (provisioning + fail closed).

Error envelopes are the ones captured live in E1.6 (ROADMAP.md, 2026-11-27).
"""

from __future__ import annotations

import json

import pytest

from video_grouper.utils import youtube_upload
from video_grouper.utils.youtube_quota_project import (
    FailKind,
    Phase,
    ProbeState,
    QuotaProjectProvisioner,
    classify_probe,
    new_project_id,
    read_quota_project,
    write_quota_project,
)

PUBLISHER_ID = "publisher.apps.googleusercontent.com"


def _err(status, code, reason, detail_reason, message="x"):
    return {
        "error": {
            "code": status,
            "status": code,
            "message": message,
            "errors": [{"reason": reason, "domain": "global"}],
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": detail_reason,
                }
            ],
        }
    }


PROJECT_NOT_FOUND = _err(400, "INVALID_ARGUMENT", "badRequest", "USER_PROJECT_DENIED")
NO_ACCESS = _err(403, "PERMISSION_DENIED", "forbidden", "USER_PROJECT_DENIED")
SERVICE_DISABLED = _err(
    403, "PERMISSION_DENIED", "accessNotConfigured", "SERVICE_DISABLED"
)
TOS = {
    "error": {
        "code": 400,
        "status": "FAILED_PRECONDITION",
        "message": "Callers must accept Terms of Service",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.PreconditionFailure",
                "violations": [{"type": "TOS", "subject": "cloud"}],
            }
        ],
    }
}


# ---------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"items": [{"id": "UC1"}]}, ProbeState.READY),
        (200, {"items": []}, ProbeState.NO_CHANNEL),
        (200, {}, ProbeState.NO_CHANNEL),
        (400, PROJECT_NOT_FOUND, ProbeState.PROJECT_NOT_FOUND),
        (403, NO_ACCESS, ProbeState.NO_ACCESS),
        (403, SERVICE_DISABLED, ProbeState.SERVICE_DISABLED),
        (
            429,
            {"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            ProbeState.QUOTA,
        ),
        (403, {"error": {"errors": [{"reason": "quotaExceeded"}]}}, ProbeState.QUOTA),
        (500, {"error": {"message": "boom"}}, ProbeState.OTHER),
    ],
)
def test_classify_probe_matches_e16_catalogue(status, body, expected):
    assert classify_probe(status, body) == expected


def test_same_detail_reason_split_by_status():
    """400 and 403 both carry USER_PROJECT_DENIED but mean different things."""
    assert classify_probe(400, PROJECT_NOT_FOUND) != classify_probe(403, NO_ACCESS)


# ---------------------------------------------------------------- sidecar


def test_sidecar_roundtrip(tmp_path):
    write_quota_project(tmp_path, "soccer-cam-abc12345", "a@example.com")
    saved = read_quota_project(tmp_path)
    assert saved["project_id"] == "soccer-cam-abc12345"
    assert saved["account"] == "a@example.com"


@pytest.mark.parametrize("content", ["not json", '{"project_id": "Bad_ID"}', "{}"])
def test_sidecar_rejects_garbage(tmp_path, content):
    (tmp_path / "quota_project.json").write_text(content)
    assert read_quota_project(tmp_path) is None


def test_new_project_id_shape():
    pid = new_project_id()
    assert pid.startswith("soccer-cam-") and len(pid) == 19
    assert pid == pid.lower()


# ---------------------------------------------------------------- provisioner


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class ScriptedSession:
    """Answers each (method, url-fragment) from a queue; the last answer repeats."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, frag), answers in self.script.items():
            if m == method and frag in url:
                status, body = answers.pop(0) if len(answers) > 1 else answers[0]
                return FakeResp(status, body)
        raise AssertionError(f"unscripted {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(setup, yt, **kw):
    clock = Clock()
    ready = []
    prov = QuotaProjectProvisioner(
        setup,
        yt,
        lambda pid, ch: ready.append((pid, ch)),
        sleep=clock.sleep,
        clock=clock,
        **kw,
    )
    prov.run()
    return prov.status, ready, clock


OP_DONE = (200, {"name": "operations/cp.1", "done": True})
EMPTY_SEARCH = (200, {})


def test_happy_path_creates_enables_and_verifies():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [(200, {"name": "operations/cp.1"})],
            ("GET", "operations/cp.1"): [OP_DONE],
            ("POST", ":enable"): [(200, {"name": "operations/acf.2", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": [{"id": "UC"}]})]})
    status, ready, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY
    assert status["has_channel"] is True
    assert ready and ready[0][0].startswith("soccer-cam-")
    create = next(c for c in setup.calls if c[0] == "POST" and "/v3/projects" in c[1])
    assert create[2]["json"]["labels"] == {"soccer-cam": "quota"}
    # Every probe carries the quota header for the new project.
    assert all(c[2]["headers"]["x-goog-user-project"] == ready[0][0] for c in yt.calls)


def test_reuses_existing_labelled_project():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [
                (
                    200,
                    {
                        "projects": [
                            {"projectId": "soccer-cam-old00001", "createTime": "2026"}
                        ]
                    },
                )
            ],
            ("POST", ":enable"): [(200, {"name": "operations/noop", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": [{"id": "UC"}]})]})
    status, ready, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY and status["reused"] is True
    assert ready[0][0] == "soccer-cam-old00001"
    assert not any(
        c[0] == "POST" and c[1].endswith("/v3/projects") for c in setup.calls
    )


def test_terms_violation_waits_then_retries_creation():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (400, TOS),
                (400, TOS),
                (200, {"name": "operations/cp.1", "done": True}),
            ],
            ("POST", ":enable"): [(200, {"name": "operations/acf.2", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": []})]})
    status, ready, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY
    assert status["has_channel"] is False  # U8: quota works, no channel yet
    assert "no YouTube channel" in status["message"]


def test_terms_never_accepted_times_out():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [(400, TOS)],
        }
    )
    status, ready, _ = _run(setup, ScriptedSession({}), terms_timeout=60)
    assert status["phase"] == Phase.FAILED
    assert status["fail_kind"] == FailKind.TERMS_TIMEOUT
    assert ready == []


def test_out_of_projects_fails_with_specific_kind():
    quota = {
        "error": {
            "code": 400,
            "status": "FAILED_PRECONDITION",
            "message": "You have exceeded your project quota",
        }
    }
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [(400, quota)],
        }
    )
    status, _, _ = _run(setup, ScriptedSession({}))
    assert status["fail_kind"] == FailKind.NO_PROJECTS_LEFT


def test_id_collision_picks_new_id():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (409, {"error": {"status": "ALREADY_EXISTS"}}),
                (200, {"name": "operations/cp.1", "done": True}),
            ],
            ("POST", ":enable"): [(200, {"name": "operations/x", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": [{"id": "UC"}]})]})
    status, _, _ = _run(setup, yt)
    creates = [
        c for c in setup.calls if c[0] == "POST" and c[1].endswith("/v3/projects")
    ]
    assert status["phase"] == Phase.READY
    assert creates[0][2]["json"]["projectId"] != creates[1][2]["json"]["projectId"]


def test_enable_waits_for_new_project_to_become_visible():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (200, {"name": "operations/cp.1", "done": True})
            ],
            ("POST", ":enable"): [
                (403, NO_ACCESS),
                (404, {}),
                (200, {"name": "operations/x", "done": True}),
            ],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": [{"id": "UC"}]})]})
    status, _, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY


def test_verify_tolerates_e16_flap_and_needs_stable_answer():
    """service_disabled -> no_access -> service_disabled -> ok, as seen live."""
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (200, {"name": "operations/cp.1", "done": True})
            ],
            ("POST", ":enable"): [(200, {"name": "operations/x", "done": True})],
        }
    )
    ok = (200, {"items": [{"id": "UC"}]})
    yt = ScriptedSession(
        {
            ("GET", "/channels"): [
                (403, SERVICE_DISABLED),
                (403, NO_ACCESS),
                (403, SERVICE_DISABLED),
                ok,
                ok,
            ]
        }
    )
    status, _, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY
    assert len(yt.calls) == 5  # one 200 alone did not decide


def test_verify_gives_up_when_project_never_usable():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (200, {"name": "operations/cp.1", "done": True})
            ],
            ("POST", ":enable"): [(200, {"name": "operations/x", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(403, NO_ACCESS)]})
    status, ready, _ = _run(setup, yt, visible_timeout=60)
    assert status["phase"] == Phase.FAILED
    assert status["fail_kind"] == FailKind.NOT_VISIBLE
    assert ready == []


def test_operation_error_fails():
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [(200, {"name": "operations/cp.1"})],
            ("GET", "operations/cp.1"): [
                (200, {"done": True, "error": {"message": "nope"}})
            ],
        }
    )
    status, _, _ = _run(setup, ScriptedSession({}))
    assert status["phase"] == Phase.FAILED and "nope" in status["message"]


# ---------------------------------------------------------------- fail closed


@pytest.fixture
def publisher(monkeypatch):
    installed = youtube_upload.EMBEDDED_CLIENT_CONFIG["installed"]
    monkeypatch.setitem(installed, "client_id", PUBLISHER_ID)
    monkeypatch.setitem(installed, "client_secret", "publisher-secret")


def _token(path, client_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
    return path


def test_publisher_token_without_project_refuses(tmp_path, publisher):
    token = _token(tmp_path / "youtube" / "token.json", PUBLISHER_ID)
    with pytest.raises(youtube_upload.YouTubeQuotaProjectMissing):
        youtube_upload.load_youtube_credentials(str(token))
    ok, message = youtube_upload.ensure_valid_token(
        str(tmp_path / "youtube" / "client_secret.json"), str(token)
    )
    assert not ok and "quota project" in message


def test_publisher_token_gets_quota_header(tmp_path, publisher):
    token = _token(tmp_path / "youtube" / "token.json", PUBLISHER_ID)
    write_quota_project(tmp_path / "youtube", "soccer-cam-abc12345", None)
    creds = youtube_upload.load_youtube_credentials(str(token))
    assert creds.quota_project_id == "soccer-cam-abc12345"
    # No client_secret.json needed for a publisher token.
    ok, _ = youtube_upload.ensure_valid_token(
        str(tmp_path / "youtube" / "client_secret.json"), str(token)
    )
    assert ok


def _secret(youtube_dir, client_id):
    (youtube_dir / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": client_id, "client_secret": "s"}})
    )


def test_byo_token_unaffected(tmp_path, publisher):
    token = _token(tmp_path / "youtube" / "token.json", "someone-elses-client")
    _secret(tmp_path / "youtube", "someone-elses-client")
    creds = youtube_upload.load_youtube_credentials(str(token))
    assert creds.quota_project_id is None


def test_publisher_token_fails_closed_even_when_build_lacks_the_client_id(
    tmp_path, monkeypatch
):
    """Review finding: a source run doesn't know the publisher id. The token
    must still refuse rather than load as BYO and bill the shared project."""
    installed = youtube_upload.EMBEDDED_CLIENT_CONFIG["installed"]
    monkeypatch.setitem(installed, "client_id", "")
    token = _token(tmp_path / "youtube" / "token.json", PUBLISHER_ID)
    with pytest.raises(youtube_upload.YouTubeQuotaProjectMissing):
        youtube_upload.load_youtube_credentials(str(token))
    write_quota_project(tmp_path / "youtube", "soccer-cam-abc12345", None)
    creds = youtube_upload.load_youtube_credentials(str(token))
    assert creds.quota_project_id == "soccer-cam-abc12345"


def test_byo_token_without_its_client_secret_is_flagged(tmp_path, publisher):
    """BYO token whose client_secret.json is gone: fail validation so the
    re-auth banner/notification fires (review finding)."""
    token = _token(tmp_path / "youtube" / "token.json", "someone-elses-client")
    ok, _ = youtube_upload.ensure_valid_token(
        str(tmp_path / "youtube" / "client_secret.json"), str(token)
    )
    assert not ok


def test_no_publisher_in_build_means_no_token_is_publisher(tmp_path, monkeypatch):
    installed = youtube_upload.EMBEDDED_CLIENT_CONFIG["installed"]
    monkeypatch.setitem(installed, "client_id", "")
    assert not youtube_upload.is_publisher_token({"client_id": ""})
    assert not youtube_upload.publisher_client_available()


def test_publisher_project_missing_apis_is_reported_not_retried():
    """Live 2026-11-27: RM not enabled on the OAuth client's own project."""
    disabled = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "Cloud Resource Manager API has not been used in project 396182566355",
            "details": [
                {
                    "reason": "SERVICE_DISABLED",
                    "metadata": {"service": "cloudresourcemanager.googleapis.com"},
                }
            ],
        }
    }
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [(403, disabled)],
            ("POST", "/v3/projects"): [(403, disabled)],
        }
    )
    status, ready, _ = _run(setup, ScriptedSession({}))
    assert status["fail_kind"] == FailKind.PUBLISHER_SETUP
    assert "isn't something you can fix" in status["message"]
    assert ready == []


def test_verify_survives_a_transient_youtube_error():
    """One 503 during verification must not fail the whole run (review finding)."""
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (200, {"name": "operations/cp.1", "done": True})
            ],
            ("POST", ":enable"): [(200, {"name": "operations/x", "done": True})],
        }
    )
    ok = (200, {"items": [{"id": "UC"}]})
    yt = ScriptedSession(
        {("GET", "/channels"): [(503, {"error": {"message": "backendError"}}), ok, ok]}
    )
    status, _, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY


def test_rate_limited_project_creation_retries_instead_of_out_of_projects():
    throttled = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded for quota metric Project create requests",
        }
    }
    setup = ScriptedSession(
        {
            ("GET", "projects:search"): [EMPTY_SEARCH],
            ("POST", "/v3/projects"): [
                (429, throttled),
                (200, {"name": "operations/cp.1", "done": True}),
            ],
            ("POST", ":enable"): [(200, {"name": "operations/x", "done": True})],
        }
    )
    yt = ScriptedSession({("GET", "/channels"): [(200, {"items": [{"id": "UC"}]})]})
    status, _, _ = _run(setup, yt)
    assert status["phase"] == Phase.READY
