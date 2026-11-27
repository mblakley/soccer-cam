"""User-owned YouTube quota project: discovery, creation, enablement, probing.

Soccer-cam signs users in to YouTube with a shared *publisher* OAuth client
(TTT's), but every API call is billed to a Google Cloud project the user owns,
named by the ``x-goog-user-project`` header. This module provisions that
project on the user's behalf and classifies the API's answers.

Background and the measurements every rule here rests on:
``.claude/plans/youtube-byo-quota/ROADMAP.md`` (E1, E1.6, E1.7).

Shape of the flow (driven by :class:`QuotaProjectProvisioner`):

1. Look for a project this account already made for soccer-cam (label
   ``soccer-cam=quota``) and reuse it -- consumer accounts have a small
   project quota.
2. Otherwise ``projects.create``. A never-used account fails with a Terms of
   Service violation; no API can accept the Terms, so the user is sent to
   Cloud Console and creation is retried until it succeeds.
3. Enable ``youtube.googleapis.com`` on the project.
4. Poll ``channels.list`` with the quota header until the answer is stable.

The Cloud scopes used for steps 1-3 come from a separate, online-only consent:
the access token lives in memory for this run and is never written to disk.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Separate consent, used once, never persisted. ``openid`` + ``email`` let the
# server check the user picked the same Google account as for YouTube -- a
# project owned by a different account answers every call with 403.
SETUP_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloudplatformprojects",
    "https://www.googleapis.com/auth/service.management",
]

YOUTUBE_SERVICE = "youtube.googleapis.com"
PROJECT_LABEL_KEY = "soccer-cam"
PROJECT_LABEL_VALUE = "quota"
PROJECT_DISPLAY_NAME = "Soccer-Cam YouTube"
# Any Cloud Console page shows the first-run Terms dialog (E1.6).
TERMS_URL = "https://console.cloud.google.com/"

RM = "https://cloudresourcemanager.googleapis.com/v3"
SU = "https://serviceusage.googleapis.com/v1"
YT = "https://youtube.googleapis.com/youtube/v3"

SIDECAR_FILENAME = "quota_project.json"

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


# --------------------------------------------------------------------------
# Sidecar: which project this install's YouTube token bills to
# --------------------------------------------------------------------------


def read_quota_project(youtube_dir: str | Path) -> dict | None:
    """Return the saved ``{"project_id", "account", ...}`` or ``None``."""
    path = Path(youtube_dir) / SIDECAR_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not _ID_RE.match(project_id):
        return None
    return data


def write_quota_project(
    youtube_dir: str | Path,
    project_id: str,
    account: str | None,
    has_channel: bool | None = None,
) -> None:
    path = Path(youtube_dir) / SIDECAR_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project_id": project_id,
                "account": account,
                # False = quota works but the account has no YouTube channel
                # (U8): uploads would fail later for an unrelated-looking reason.
                "has_channel": has_channel,
                "provisioned_at": int(time.time()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_quota_project(youtube_dir: str | Path) -> None:
    (Path(youtube_dir) / SIDECAR_FILENAME).unlink(missing_ok=True)


def new_project_id() -> str:
    """``soccer-cam-<8>``: 19 chars, inside GCP's 6-30 and starts with a letter."""
    alphabet = string.ascii_lowercase + string.digits
    return "soccer-cam-" + "".join(secrets.choice(alphabet) for _ in range(8))


# --------------------------------------------------------------------------
# Classifying channels.list answers (the E1.6 state catalogue)
# --------------------------------------------------------------------------


class ProbeState(StrEnum):
    READY = "ready"  # 200, account has a channel
    NO_CHANNEL = "no_channel"  # 200, empty items (U8) -- quota works, no channel
    PROJECT_NOT_FOUND = "project_not_found"  # 400 USER_PROJECT_DENIED
    NO_ACCESS = "no_access"  # 403 USER_PROJECT_DENIED
    SERVICE_DISABLED = "service_disabled"  # 403 SERVICE_DISABLED
    QUOTA = "quota"  # quota / rate limit
    OTHER = "other"


def _error_reasons(body: Any) -> tuple[str | None, set[str]]:
    """``(errors[0].reason, {details[].reason})`` from a Google error body."""
    if not isinstance(body, dict):
        return None, set()
    err = body.get("error")
    if not isinstance(err, dict):
        return None, set()
    errors = err.get("errors") or [{}]
    first = errors[0].get("reason") if isinstance(errors[0], dict) else None
    details = {
        str(d["reason"])
        for d in err.get("details") or []
        if isinstance(d, dict) and d.get("reason")
    }
    return (str(first) if first else None), details


def classify_probe(status: int, body: Any) -> ProbeState:
    """Map a ``channels.list?mine=true`` response to a :class:`ProbeState`.

    Keys on HTTP status + reason, never on one alone: a 400 and a 403 both
    carry ``USER_PROJECT_DENIED`` but mean "not created yet" vs "no access".
    """
    if status == 200:
        items = body.get("items") if isinstance(body, dict) else None
        return ProbeState.READY if items else ProbeState.NO_CHANNEL
    reason, details = _error_reasons(body)
    if "SERVICE_DISABLED" in details or reason == "accessNotConfigured":
        return ProbeState.SERVICE_DISABLED
    if "USER_PROJECT_DENIED" in details:
        return ProbeState.PROJECT_NOT_FOUND if status == 400 else ProbeState.NO_ACCESS
    if (
        status == 429
        or reason in {"quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"}
        or "RATE_LIMIT_EXCEEDED" in details
    ):
        return ProbeState.QUOTA
    return ProbeState.OTHER


def probe(session: Any, project_id: str) -> tuple[ProbeState, int, Any]:
    """One ``channels.list`` call billed to ``project_id``."""
    resp = session.get(
        f"{YT}/channels",
        params={"part": "id", "mine": "true"},
        headers={"x-goog-user-project": project_id},
        timeout=60,
    )
    body = _json(resp)
    return classify_probe(resp.status_code, body), resp.status_code, body


def _json(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": (resp.text or "")[:500]}


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


class Phase(StrEnum):
    STARTING = "starting"
    FINDING = "finding"
    CREATING = "creating"
    NEEDS_TERMS = "needs_terms"
    WAITING_FOR_PROJECT = "waiting_for_project"
    ENABLING = "enabling"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"


class FailKind(StrEnum):
    NO_PROJECTS_LEFT = "no_projects_left"
    TERMS_TIMEOUT = "terms_timeout"
    NOT_VISIBLE = "not_visible"
    API_ERROR = "api_error"
    PUBLISHER_SETUP = "publisher_setup"


class ProvisionError(Exception):
    def __init__(self, kind: FailKind, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class ProvisionStatus:
    phase: Phase = Phase.STARTING
    message: str = ""
    project_id: str | None = None
    reused: bool = False
    has_channel: bool | None = None
    fail_kind: FailKind | None = None
    terms_url: str | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "phase": str(self.phase),
            "message": self.message,
            "project_id": self.project_id,
            "reused": self.reused,
            "has_channel": self.has_channel,
            "fail_kind": str(self.fail_kind) if self.fail_kind else None,
            "terms_url": self.terms_url,
            "elapsed": int(time.time() - self.started_at),
            "done": self.phase in (Phase.READY, Phase.FAILED),
        }


def _is_terms_violation(body: Any) -> bool:
    """F6: ``projects.create`` fails with a ``TOS`` precondition violation."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return False
    for d in err.get("details") or []:
        for v in (d or {}).get("violations") or []:
            if str(v.get("type", "")).upper() == "TOS":
                return True
    return bool(re.search(r"terms of service", str(err.get("message", "")), re.I))


def _is_project_quota(body: Any) -> bool:
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return False
    text = json.dumps(err).lower()
    return "quota" in text and "project" in text


def _publisher_api_disabled(status: int, body: Any) -> bool:
    """403 SERVICE_DISABLED for Resource Manager / Service Usage itself.

    Calls made before the user's project exists bill the OAuth client's own
    project, so both APIs must be enabled there (found live 2026-11-27). The
    user can't fix this; never mistake it for propagation lag.
    """
    if status != 403 or not isinstance(body, dict):
        return False
    text = json.dumps(body.get("error") or {})
    return "SERVICE_DISABLED" in text and (
        "cloudresourcemanager.googleapis.com" in text
        or "serviceusage.googleapis.com" in text
    )


_PUBLISHER_SETUP_MSG = (
    "Soccer-cam's Google sign-in app is missing a setting on Google's side "
    "(Cloud Resource Manager / Service Usage API not enabled on the app's "
    "project). This isn't something you can fix -- please report it."
)


def _err_text(status: int, body: Any) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return f"{status} {err.get('status', '')}: {err.get('message', '')}".strip()
    return f"HTTP {status}"


class QuotaProjectProvisioner:
    """Run the create/enable/verify flow in a background thread.

    ``setup_session`` carries the one-time Cloud-scoped token; ``yt_session``
    carries the user's YouTube token. Both are ``requests``-compatible
    sessions (``google.auth.transport.requests.AuthorizedSession`` in
    production). ``on_ready(project_id)`` persists the result.

    Timeouts are generous but bounded: a wizard poll that never ends would
    keep calling Google on the user's behalf indefinitely.
    """

    def __init__(
        self,
        setup_session: Any,
        yt_session: Any,
        on_ready: Callable[[str, bool], None],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 5.0,
        terms_timeout: float = 20 * 60,
        visible_timeout: float = 6 * 60,
        stable_polls: int = 2,
    ):
        self.setup = setup_session
        self.yt = yt_session
        self.on_ready = on_ready
        self._sleep = sleep
        self._clock = clock
        self.poll_seconds = poll_seconds
        self.terms_timeout = terms_timeout
        self.visible_timeout = visible_timeout
        self.stable_polls = stable_polls
        self._lock = threading.Lock()
        self._status = ProvisionStatus()
        self._thread: threading.Thread | None = None

    # -- status ------------------------------------------------------------

    @property
    def status(self) -> dict:
        with self._lock:
            return self._status.as_dict()

    def _set(self, phase: Phase, message: str = "", **fields: Any) -> None:
        with self._lock:
            self._status.phase = phase
            self._status.message = message
            for k, v in fields.items():
                setattr(self._status, k, v)
            self._status.updated_at = time.time()
        logger.info("YOUTUBE_QUOTA: %s %s", phase, message)

    # -- driver ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.run, name="youtube-quota-provisioner", daemon=True
        )
        self._thread.start()

    def run(self) -> None:
        try:
            project_id, reused = self._find_or_create()
            self._enable(project_id)
            has_channel = self._verify(project_id)
            self.on_ready(project_id, has_channel)
            msg = (
                "Uploads will use your own Google Cloud project."
                if has_channel
                else "Quota is set up, but this Google account has no YouTube "
                "channel yet. Create one at youtube.com before the first upload."
            )
            self._set(Phase.READY, msg, has_channel=has_channel, reused=reused)
        except ProvisionError as exc:
            self._set(Phase.FAILED, str(exc), fail_kind=exc.kind)
        except Exception as exc:  # noqa: BLE001 -- surface, never hang the wizard
            logger.exception("YOUTUBE_QUOTA: provisioning crashed")
            self._set(
                Phase.FAILED, f"Unexpected error: {exc}", fail_kind=FailKind.API_ERROR
            )

    # -- steps -------------------------------------------------------------

    def _find_existing(self) -> str | None:
        resp = self.setup.get(
            f"{RM}/projects:search",
            params={
                "query": f"labels.{PROJECT_LABEL_KEY}:{PROJECT_LABEL_VALUE} "
                "state:ACTIVE"
            },
            timeout=60,
        )
        body = _json(resp)
        if resp.status_code != 200:
            # A never-used account can't search either; creation will say why.
            logger.info(
                "YOUTUBE_QUOTA: search failed: %s", _err_text(resp.status_code, body)
            )
            return None
        projects = [p for p in body.get("projects") or [] if p.get("projectId")]
        projects.sort(key=lambda p: p.get("createTime", ""))
        return projects[0]["projectId"] if projects else None

    def _find_or_create(self) -> tuple[str, bool]:
        self._set(Phase.FINDING, "Looking for an existing soccer-cam project.")
        existing = self._find_existing()
        if existing:
            self._set(
                Phase.ENABLING,
                f"Reusing your existing project {existing}.",
                project_id=existing,
            )
            return existing, True

        project_id = new_project_id()
        self._set(
            Phase.CREATING, "Creating your Google Cloud project.", project_id=project_id
        )
        deadline = self._clock() + self.terms_timeout
        while True:
            resp = self.setup.post(
                f"{RM}/projects",
                json={
                    "projectId": project_id,
                    "displayName": PROJECT_DISPLAY_NAME,
                    "labels": {PROJECT_LABEL_KEY: PROJECT_LABEL_VALUE},
                },
                timeout=60,
            )
            body = _json(resp)
            if _publisher_api_disabled(resp.status_code, body):
                raise ProvisionError(FailKind.PUBLISHER_SETUP, _PUBLISHER_SETUP_MSG)
            if resp.status_code == 200:
                self._wait_operation(f"{RM}/{body['name']}", body, "project creation")
                return project_id, False
            if _is_terms_violation(body):
                if self._clock() > deadline:
                    raise ProvisionError(
                        FailKind.TERMS_TIMEOUT,
                        "Google Cloud's Terms of Service were not accepted in time. "
                        "Accept them, then start this step again.",
                    )
                self._set(
                    Phase.NEEDS_TERMS,
                    "Accept Google Cloud's Terms of Service to continue. This "
                    "page carries on by itself once you have.",
                    terms_url=TERMS_URL,
                )
                self._sleep(self.poll_seconds * 2)
                continue
            if resp.status_code == 429:
                if self._clock() > deadline:
                    raise ProvisionError(
                        FailKind.API_ERROR,
                        "Google Cloud is rate-limiting project creation. Try again "
                        "in a few minutes.",
                    )
                self._sleep(self.poll_seconds * 6)
                continue
            if resp.status_code == 409:
                project_id = new_project_id()  # id taken; ids are global
                self._set(
                    Phase.CREATING,
                    "Creating your Google Cloud project.",
                    project_id=project_id,
                )
                continue
            if _is_project_quota(body):
                raise ProvisionError(
                    FailKind.NO_PROJECTS_LEFT,
                    "This Google account has used up its Google Cloud project "
                    "allowance. Delete an unused project at "
                    "console.cloud.google.com/cloud-resource-manager, then retry.",
                )
            raise ProvisionError(
                FailKind.API_ERROR,
                f"Google Cloud refused to create the project ({_err_text(resp.status_code, body)}).",
            )

    def _wait_operation(self, url: str, op: dict, what: str) -> None:
        deadline = self._clock() + self.visible_timeout
        while not op.get("done"):
            if self._clock() > deadline:
                raise ProvisionError(
                    FailKind.API_ERROR, f"Timed out waiting for {what}."
                )
            self._sleep(self.poll_seconds)
            resp = self.setup.get(url, timeout=60)
            op = _json(resp)
            if resp.status_code != 200:
                raise ProvisionError(
                    FailKind.API_ERROR,
                    f"Could not check {what} ({_err_text(resp.status_code, op)}).",
                )
        if op.get("error"):
            raise ProvisionError(
                FailKind.API_ERROR,
                f"{what.capitalize()} failed: {op['error'].get('message', op['error'])}",
            )

    def _enable(self, project_id: str) -> None:
        self._set(
            Phase.ENABLING, "Turning on the YouTube Data API.", project_id=project_id
        )
        url = f"{SU}/projects/{project_id}/services/{YOUTUBE_SERVICE}:enable"
        # A project seconds old is not yet visible to Service Usage (E1.6 saw
        # ~92 s before the YouTube side noticed it), so 403/404 means wait.
        deadline = self._clock() + self.visible_timeout
        while True:
            resp = self.setup.post(url, json={}, timeout=60)
            body = _json(resp)
            if _publisher_api_disabled(resp.status_code, body):
                raise ProvisionError(FailKind.PUBLISHER_SETUP, _PUBLISHER_SETUP_MSG)
            if resp.status_code == 200:
                self._wait_operation(f"{SU}/{body['name']}", body, "enabling the API")
                return
            if resp.status_code in (403, 404) and self._clock() < deadline:
                self._set(
                    Phase.WAITING_FOR_PROJECT,
                    "Waiting for Google to finish setting up the project.",
                )
                self._sleep(self.poll_seconds)
                continue
            raise ProvisionError(
                FailKind.API_ERROR,
                f"Could not turn on the YouTube Data API ({_err_text(resp.status_code, body)}).",
            )

    def _verify(self, project_id: str) -> bool:
        """Poll until YouTube answers 200 through the project, ``stable_polls`` in a row.

        States are not monotonic (E1.6 saw service_disabled -> no_access ->
        service_disabled), so a single answer never decides.
        """
        self._set(Phase.VERIFYING, "Checking YouTube can bill your project.")
        deadline = self._clock() + self.visible_timeout
        last: ProbeState | None = None
        streak = 0
        while True:
            try:
                state, status, body = probe(self.yt, project_id)
            except Exception as exc:  # noqa: BLE001 -- network blip; retry
                logger.info("YOUTUBE_QUOTA: probe failed, retrying: %s", exc)
                state, status, body = ProbeState.OTHER, 0, {}
            streak = streak + 1 if state == last else 1
            last = state
            if (
                state in (ProbeState.READY, ProbeState.NO_CHANNEL)
                and streak >= self.stable_polls
            ):
                return state == ProbeState.READY
            if state == ProbeState.QUOTA:
                # Quota trouble on a project we can reach still proves billing works.
                return True
            # A single odd answer (5xx, refresh race) never decides, per the
            # E1.6 rule; only a sustained one fails, at the deadline below.
            if self._clock() > deadline:
                if state == ProbeState.OTHER:
                    raise ProvisionError(
                        FailKind.API_ERROR,
                        "YouTube kept returning an unexpected answer "
                        f"({_err_text(status, body)}).",
                    )
                raise ProvisionError(
                    FailKind.NOT_VISIBLE,
                    f"YouTube still can't use project {project_id} (last answer: "
                    f"{state}). If you picked a different Google account for "
                    "YouTube and for the project, connect again with the same one.",
                )
            self._sleep(self.poll_seconds)
