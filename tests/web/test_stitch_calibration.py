"""Tests for the seam-calibration tool at ``/stitch``.

The camera transport is faked; the *metric* is not. `seam_metric` runs for
real against a synthetic panorama, because the whole claim of the tool is that
an operator descends the same objective the automatic solver does, and a
mocked score would test nothing.

`apply_calibration` is faked deliberately and permanently: the write path is
hardware-verified in its own right (#135), it sets vendor scalars and writes a
warp mesh, and a unit test has no business doing either. What is tested here is
that the button reaches it with the operator's curve, in the right shape, and
refuses the cases that would double-correct.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_grouper.utils.stitch_remap import StitchProfile
from video_grouper.web import stitch_calibration as sc

VPE_DIR = Path(__file__).resolve().parents[2] / "reolink-firmware-patching" / "vpe"
sys.path.insert(0, str(VPE_DIR))

import seam_metric  # noqa: E402

# Small enough that detection is ~1 s instead of the 37-50 s a real
# 7680x2160 game frame costs, and still a genuine
# two-sensor butt-join with a 256-px blend and sloped structure across it.
W, H = 2048, 900
SEAM = W // 2
BLEND = 256
_LINES = (
    (-0.30, 760),
    (-0.16, 300),
    (-0.05, 620),
    (0.07, 180),
    (0.19, 500),
    (0.30, 80),
)


def _panorama(disparity: int = 6) -> np.ndarray:
    rng = np.random.default_rng(3)
    w = W + 512
    img = 110 + 6 * cv2.GaussianBlur(rng.normal(0.0, 1.0, (H, w)), (0, 0), 5.0)
    xs = np.arange(w)
    for m, y0 in _LINES:
        ys = y0 + m * (xs - w / 2)
        for off, amp in ((-1, 30), (0, 55), (1, 30)):
            img[np.clip(np.round(ys + off).astype(int), 0, H - 1), xs] += amp
    img = np.clip(img, 0, 255)
    left = img[:, 256 : 256 + W]
    right = np.roll(img, -disparity, axis=1)[:, 256 : 256 + W]
    out = left.copy()
    lo, hi = SEAM - BLEND // 2, SEAM + BLEND // 2
    alpha = np.linspace(0.0, 1.0, hi - lo)[None, :]
    out[:, hi:] = right[:, hi:]
    out[:, lo:hi] = left[:, lo:hi] * (1 - alpha) + right[:, lo:hi] * alpha
    return np.dstack([np.clip(out, 0, 255).astype(np.uint8)] * 3)


class _Scalars:
    def __init__(self, distance, x, y):
        self._d = {"distance": distance, "stitchXMove": x, "stitchYMove": y}

    def to_api(self):
        return dict(self._d)


def _fake_camera(calls: list) -> types.ModuleType:
    """A stand-in for `stitch_apply` that records instead of transmitting."""
    mod = types.ModuleType("fake_stitch_apply")

    def snap(host, user, password, out: Path):
        calls.append(("snap", host, user))
        cv2.imwrite(str(out), _panorama())
        return out

    def get_stitch(host, user, password):
        calls.append(("get_stitch", host, user))
        return _Scalars(8.0, 3, 0), _Scalars(8.0, 0, 0)

    def apply_calibration(anchors, **kw):
        calls.append(("apply", list(anchors), kw))
        return {
            "host": kw.get("host"),
            "stages": [{"surface": "camera_mesh", "state": "applied"}],
        }

    mod.snap = snap  # type: ignore[attr-defined]
    mod.get_stitch = get_stitch  # type: ignore[attr-defined]
    mod.apply_calibration = apply_calibration  # type: ignore[attr-defined]
    return mod


CONFIG = """\
[CAMERA.duo3]
type = reolink
device_ip = 10.0.0.9
username = admin
password = hunter2

[STORAGE]
path = {storage}
min_free_gb = 2.0

[RECORDING]
min_duration = 60
max_duration = 3600

[PROCESSING]
max_concurrent_downloads = 2

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
"""


@pytest.fixture
def calls() -> list:
    return []


@pytest.fixture
def client(tmp_path, calls, monkeypatch):
    config_path = tmp_path / "config.ini"
    config_path.write_text(CONFIG.format(storage=str(tmp_path).replace("\\", "/")))
    # Real metric, fake transport.
    sc._toolkit_cache.clear()
    sc._toolkit_cache.update(
        {"metric": seam_metric, "camera": _fake_camera(calls), "errors": []}
    )
    sc._session.__init__()  # type: ignore[misc] -- fresh session per test
    app = FastAPI()
    app.include_router(sc.build_router(config_path, tmp_path))
    with TestClient(app) as c:
        yield c
    sc._toolkit_cache.clear()


def _snap(client) -> dict:
    r = client.post("/stitch/snap")
    assert r.status_code == 200, r.text
    # Detection runs in a background thread; wait for it the way the page does.
    for _ in range(200):
        st = client.get("/stitch/state").json()
        if st["metric_state"] in ("ready", "failed"):
            return st
        import time

        time.sleep(0.1)
    raise AssertionError("baseline detection never finished")


def _flat(dx: float) -> list[list[float]]:
    return [[0, dx], [225, dx], [450, dx], [675, dx], [899, dx]]


# -- page + session ----------------------------------------------------------


def test_page_renders_and_states_the_constraints_it_must_state(client):
    body = client.get("/stitch").text
    assert body.startswith("<!DOCTYPE html>")
    # The two things the design says must be visible in the interface, not
    # merely true of the code.
    assert "already a mixture" in body, "the fused-JPEG caveat must be UI text"
    assert "the window is inference" in body.lower()
    assert "camera can only move the left" in body.lower()


def test_the_surface_selector_flips_the_presentation_and_only_the_presentation(client):
    """The client half of the contract, pinned in the page source.

    The stored-anchors half is covered by
    `test_the_stored_curve_does_not_depend_on_the_chosen_surface`; this pins
    the sign rule that decides which half the operator sees move. `dx` means
    "the right half moves right", so the camera surface -- which can only warp
    the LEFT half -- has to realise the same relative displacement with the
    opposite sign. Getting that backwards would show the operator a correction
    moving the wrong way while storing a curve that is right, which is the
    hardest kind of bug to see.

    (Verified live in Chrome as well: with the camera surface the right half
    dims and signFor is left=-1 / right=0; switching to downstream dims the
    left half and gives left=0 / right=+1, with byte-identical anchors.)
    """
    body = client.get("/stitch").text
    assert (
        "function movingSide() { return S.surface === 'downstream' ? 'right' : 'left'; }"
        in body
    )
    assert "return side === 'right' ? 1 : -1;" in body
    # Both directional captions must exist, or the flip is invisible.
    assert "You are moving the <b>LEFT</b> image" in body
    assert "You are moving the <b>RIGHT</b> image" in body


def test_state_before_any_snapshot_is_honest_about_having_nothing(client):
    st = client.get("/stitch/state").json()
    assert st["has_snapshot"] is False
    assert st["metric_state"] == "idle"
    assert st["baseline"] is None


def test_snap_fetches_the_still_and_both_scalar_blocks(client, calls):
    st = _snap(client)
    assert st["has_snapshot"] is True
    assert st["camera"] == "duo3"
    assert st["host"] == "10.0.0.9"
    assert ("snap", "10.0.0.9", "admin") in calls
    # Both the live values AND the factory block -- "confirm what the camera
    # already decided" is not a real action without the second one.
    assert st["scalars"]["current"] == {
        "distance": 8.0,
        "stitchXMove": 3,
        "stitchYMove": 0,
    }
    assert st["scalars"]["factory"]["stitchXMove"] == 0


def test_only_the_seam_strips_cross_the_wire_not_the_panorama(client):
    _snap(client)
    left = client.get("/stitch/half.jpg?side=left&v=1")
    right = client.get("/stitch/half.jpg?side=right&v=1")
    assert left.status_code == right.status_code == 200
    assert left.headers["content-type"] == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(left.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (H, sc.STRIP_W, 3)
    # The point of the split: a fraction of the frame, not the frame.
    assert len(left.content) + len(right.content) < 400_000


def test_half_before_a_snapshot_is_a_404_not_a_stale_frame(client):
    assert client.get("/stitch/half.jpg?side=left").status_code == 404


# -- the live score ----------------------------------------------------------


def test_measure_scores_a_candidate_curve_against_the_real_metric(client):
    st = _snap(client)
    assert st["metric_state"] == "ready", st["metric_error"]
    base = st["baseline"]
    assert base["scr"]["n"] >= 6, "fixture must present structure to measure"

    good = client.post("/stitch/measure", json={"dx_anchors": _flat(6)}).json()
    bad = client.post("/stitch/measure", json={"dx_anchors": _flat(-6)}).json()

    assert good["scr"]["p50"] < base["scr"]["p50"], "dx=+6 must close a +6 seam"
    assert bad["scr"]["p50"] > base["scr"]["p50"], "dx=-6 must open it further"
    assert good["baseline"]["scr"]["p50"] == base["scr"]["p50"]


def test_measure_suggests_a_dx_in_the_sign_the_curve_uses(client):
    """The trap this exists to defuse.

    `ScrResult.implied_dx` is the misregistration in the image, which is the
    opposite sense to a `dx_anchors` value. Handed over unnegated it would
    double the error, which presents as "the tool doesn't work".
    """
    st = _snap(client)
    suggested = st["baseline"]["scr"]["suggested_dx"]
    assert suggested == pytest.approx(6, abs=1.5), suggested
    scored = client.post(
        "/stitch/measure", json={"dx_anchors": _flat(suggested)}
    ).json()
    assert scored["scr"]["p50"] < st["baseline"]["scr"]["p50"] * 0.5


def test_measure_before_a_snapshot_refuses_rather_than_inventing_numbers(client):
    r = client.post("/stitch/measure", json={"dx_anchors": _flat(0)})
    assert r.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"dx_anchors": []},
        {"dx_anchors": [[0, 0], [0, 3]]},  # non-increasing rows
        {"dx_anchors": [[0, 0], [10, "x"]]},
        {"dx_anchors": _flat(200)},  # beyond anything physical
        {},
    ],
)
def test_a_curve_that_cannot_be_applied_is_refused_at_authoring_time(client, payload):
    _snap(client)
    assert client.post("/stitch/measure", json=payload).status_code == 400


def test_the_frame_is_asked_whether_it_can_constrain_anything(client):
    """A number that does not move with the curve is not a measurement.

    On real Duo 3 tripod frames SCR sits at 27-36 px and shifts by 1-15% as dx
    sweeps its whole plausible range, while the seam is visibly registered --
    and every coverage gate passes while that happens. So the frame gets swept
    once and the verdict leads the panel. Here the fixture *is* misregistered,
    so the verdict must be the positive one.
    """
    st = _snap(client)
    q = st["quality"]
    assert q["usable"] is True, q["reason"]
    assert q["best_dx"] == pytest.approx(6, abs=2)
    assert q["gain"] > 0.2
    assert len(q["sweep"]) == 17


def test_an_unconstraining_frame_says_so_instead_of_reporting_a_number(
    client, tmp_path
):
    """Flat texture: structures get paired, a score comes out, and it is junk."""
    flat = np.dstack([np.full((H, W), 120, np.uint8)] * 3)
    rng = np.random.default_rng(1)
    flat = np.clip(flat + rng.normal(0, 6, flat.shape), 0, 255).astype(np.uint8)
    path = tmp_path / "flat.png"
    cv2.imwrite(str(path), flat)

    r = client.post("/stitch/open", json={"path": str(path)})
    assert r.status_code == 200
    for _ in range(200):
        st = client.get("/stitch/state").json()
        if st["metric_state"] in ("ready", "failed"):
            break
        import time

        time.sleep(0.1)
    assert st["quality"]["usable"] is False
    assert (
        "does not constrain" in st["quality"]["reason"]
        or "no structure" in (st["quality"]["reason"])
    )


# -- the second door: a frame from a recorded game ---------------------------


def test_a_frame_can_be_opened_from_a_file_not_only_from_the_camera(client, tmp_path):
    """The door most calibrations will actually use.

    A camera indoors on a bench sees a scene 0.3 m away, where parallax is tens
    of pixels; a still from a game is the representative input, and it needs no
    camera access at all.
    """
    path = tmp_path / "game_frame.jpg"
    cv2.imwrite(str(path), _panorama())
    r = client.post(
        "/stitch/open",
        json={"path": str(path), "deployment": "heat-fairport-2025.07.22"},
    )
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["has_snapshot"] is True
    assert st["camera"] == "heat-fairport-2025.07.22"
    assert st["source_path"] == str(path)
    # No camera was touched to get here.
    assert st["scalars"]["current"] is None
    assert client.get("/stitch/half.jpg?side=right").status_code == 200


def test_opening_a_missing_or_undecodable_file_says_which(client, tmp_path):
    assert client.post("/stitch/open", json={"path": ""}).status_code == 400
    assert (
        client.post(
            "/stitch/open", json={"path": str(tmp_path / "nope.jpg")}
        ).status_code
        == 404
    )
    junk = tmp_path / "junk.jpg"
    junk.write_bytes(b"not a jpeg")
    assert client.post("/stitch/open", json={"path": str(junk)}).status_code == 415


def test_a_frame_too_narrow_to_carry_a_seam_is_refused(client, tmp_path):
    tiny = tmp_path / "tiny.png"
    cv2.imwrite(str(tiny), np.zeros((64, 200, 3), np.uint8))
    r = client.post("/stitch/open", json={"path": str(tiny)})
    assert r.status_code == 415
    assert "too narrow" in r.json()["detail"]


def test_frames_can_be_listed_so_the_operator_picks_a_name(client, tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    for name in ("a.jpg", "b.png", "c.mp4", "notes.txt"):
        (d / name).write_bytes(b"x")
    got = client.get("/stitch/frames", params={"dir": str(d)}).json()
    assert got["files"] == ["a.jpg", "b.png", "c.mp4"]
    assert client.get("/stitch/frames", params={"dir": ""}).status_code == 400
    assert (
        client.get("/stitch/frames", params={"dir": str(tmp_path / "nope")}).status_code
        == 404
    )


# -- the artifact ------------------------------------------------------------


def test_save_writes_a_v2_artifact_a_v1_reader_still_understands(client, tmp_path):
    _snap(client)
    r = client.post(
        "/stitch/save",
        json={
            "dx_anchors": _flat(6.25),
            "correction_owner": "camera_mesh",
            "subject_distance_m": 45,
            "basis": "far touchline midpoint",
        },
    )
    assert r.status_code == 200, r.text
    written = json.loads((tmp_path / "stitch_profile.json").read_text())

    assert written["schema"] == "seam_calibration/2"
    assert written["correction_owner"] == "camera_mesh"
    assert written["sense"]["dx_means"].startswith("px the RIGHT half must move right")
    # Geometry recorded is the geometry measured, not a hardcoded panorama --
    # build_dx_lookup rescales by it, so a lie here scales the correction.
    assert written["source_width"] == W and written["source_height"] == H
    assert written["calibrated_for"]["subject_distance_m"] == 45

    # The whole point of extending v1 in place.
    profile = StitchProfile.from_dict(written)
    assert profile.seam_x == SEAM
    assert profile.dx_anchors[0][1] == 6, "sub-pixel anchors round, they don't truncate"


def test_every_save_is_also_filed_under_its_deployment(client, tmp_path):
    """Whether the seam offset is per-camera or per-tripod-placement is still
    being measured. A single overwritten profile answers it never; an
    append-only history keyed by deployment answers it either way."""
    _snap(client)
    for label in ("dome-2026.03.21", "fairport-2025.07.22"):
        r = client.post(
            "/stitch/save",
            json={
                "dx_anchors": _flat(4),
                "correction_owner": "downstream",
                "deployment": label,
            },
        )
        assert r.status_code == 200, r.text
    archived = sorted(p.name for p in (tmp_path / "stitch_calibrations").iterdir())
    assert len(archived) == 2
    assert archived[0].startswith("dome-2026.03.21-")
    assert archived[1].startswith("fairport-2025.07.22-")
    latest = json.loads((tmp_path / "stitch_profile.json").read_text())
    assert latest["provenance"]["deployment"] == "fairport-2025.07.22"


def test_the_stored_curve_does_not_depend_on_the_chosen_surface(client, tmp_path):
    """The surface selector changes presentation, never the artifact.

    Flipping which half is draggable and inverting the displayed sense is a UI
    affordance; if it leaked into the stored anchors, the same physical
    correction would be recorded two different ways and one of them would be
    wrong.
    """
    _snap(client)
    stored = {}
    for owner in ("camera_mesh", "downstream", "camera_scalars+downstream"):
        client.post(
            "/stitch/save", json={"dx_anchors": _flat(4.5), "correction_owner": owner}
        )
        stored[owner] = json.loads((tmp_path / "stitch_profile.json").read_text())[
            "dx_anchors"
        ]
    assert len({json.dumps(v) for v in stored.values()}) == 1, stored


def test_save_refuses_an_owner_that_cannot_carry_a_curve(client):
    _snap(client)
    r = client.post(
        "/stitch/save",
        json={"dx_anchors": _flat(6), "correction_owner": "camera_scalars"},
    )
    assert r.status_code == 400
    assert "cannot express a per-row shear" in r.json()["detail"]


# -- apply -------------------------------------------------------------------


def test_apply_hands_the_operators_curve_to_the_ordered_sequence(client, calls):
    _snap(client)
    r = client.post(
        "/stitch/apply",
        json={"dx_anchors": _flat(3.5), "correction_owner": "camera_mesh"},
    )
    assert r.status_code == 200, r.text
    applied = [c for c in calls if c[0] == "apply"]
    assert len(applied) == 1
    anchors, kw = applied[0][1], applied[0][2]
    assert anchors == [
        (0.0, 3.5),
        (225.0, 3.5),
        (450.0, 3.5),
        (675.0, 3.5),
        (899.0, 3.5),
    ]
    assert kw["host"] == "10.0.0.9"
    assert kw["password"] == "hunter2"
    # Never guessed at: the scalars are the operator's to change explicitly.
    assert kw["scalars"] is None
    assert kw["calibration_id"].startswith("duo3-")


def test_apply_refuses_the_downstream_owner_because_that_is_the_double_correct(client):
    _snap(client)
    r = client.post(
        "/stitch/apply", json={"dx_anchors": _flat(3), "correction_owner": "downstream"}
    )
    assert r.status_code == 400
    assert "double-correction" in r.json()["detail"]


def test_apply_dry_run_is_passed_through_rather_than_simulated_here(client, calls):
    _snap(client)
    client.post(
        "/stitch/apply",
        json={
            "dx_anchors": _flat(2),
            "correction_owner": "camera_mesh",
            "dry_run": True,
        },
    )
    assert [c for c in calls if c[0] == "apply"][0][2]["dry_run"] is True


def test_apply_surfaces_a_refusal_instead_of_reporting_success(client, tmp_path, calls):
    _snap(client)

    def boom(anchors, **kw):
        raise RuntimeError("the live mesh changed between the dump and the write")

    sc._toolkit_cache["camera"].apply_calibration = boom
    r = client.post(
        "/stitch/apply",
        json={"dx_anchors": _flat(3), "correction_owner": "camera_mesh"},
    )
    assert r.status_code == 502
    assert "live mesh changed" in r.json()["detail"]


# -- degradation -------------------------------------------------------------


def test_without_the_toolkit_the_page_still_loads_and_says_why(client):
    sc._toolkit_cache.clear()
    sc._toolkit_cache.update(
        {"metric": None, "camera": None, "errors": ["seam_metric: nope"]}
    )
    body = client.get("/stitch").text
    assert "Calibration toolkit unavailable" in body
    assert "seam_metric: nope" in body
    assert client.post("/stitch/snap").status_code == 503


def test_a_non_reolink_camera_is_refused_with_a_reason(tmp_path, calls):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        CONFIG.format(storage=str(tmp_path).replace("\\", "/")).replace(
            "type = reolink", "type = dahua"
        )
    )
    sc._toolkit_cache.clear()
    sc._toolkit_cache.update(
        {"metric": seam_metric, "camera": _fake_camera(calls), "errors": []}
    )
    sc._session.__init__()  # type: ignore[misc]
    app = FastAPI()
    app.include_router(sc.build_router(config_path, tmp_path))
    with TestClient(app) as c:
        r = c.post("/stitch/snap")
    assert r.status_code == 400
    assert "dual-lens" in r.json()["detail"]
    sc._toolkit_cache.clear()
