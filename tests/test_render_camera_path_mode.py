"""The dumb renderer executes camera_path/1 commands with feasibility clamps only.

The dumb-renderer contract (2026-07-09/10, single homegrown path): the renderer
has NO camera brain — the view centers where the planner said (clamped to
projection feasibility) at the planner's hfov."""

from __future__ import annotations

import json

import numpy as np
import pytest

from video_grouper.inference.cylindrical_view import pixel_to_yaw_pitch
from video_grouper.pipeline.steps.render import (
    RenderStepConfig,
    _command_for,
    _frame_view,
    _load_commands,
    _resolve_geometry,
)

_SRC_W, _SRC_H = 7680, 2160
_POLY = np.asarray(
    [
        [270.8, 1071.9],
        [2674.9, 1460.6],
        [3749.7, 1530.2],
        [5468.7, 1481.1],
        [7339.5, 1215.1],
        [5337.4, 327.3],
        [4397.9, 204.6],
        [3803.1, 171.8],
        [3277.9, 175.9],
        [2227.7, 261.8],
    ],
    dtype=np.float32,
)


def _geom(cfg: RenderStepConfig):
    return _resolve_geometry(_SRC_W, _SRC_H, cfg, _POLY)


def test_command_drives_view_yaw():
    cfg = RenderStepConfig(render_zoom_scale=1.0)
    geom = _geom(cfg)
    cx, cy, hfov = 5000.0, 1200.0, 50.0
    _params, view_yaw = _frame_view(
        (cx, cy, hfov), geom, cfg, -90.0, 90.0, _SRC_W, _SRC_H, 1920, 1080
    )
    want_yaw, _ = pixel_to_yaw_pitch(cx, cy, _SRC_W, _SRC_H, geom.src_hfov_deg)
    assert view_yaw == pytest.approx(want_yaw, abs=0.11)


def test_command_yaw_clamped_to_feasible_range():
    cfg = RenderStepConfig(render_zoom_scale=1.0)
    geom = _geom(cfg)
    _params, view_yaw = _frame_view(
        (-2000.0, 1200.0, 50.0), geom, cfg, -10.0, 10.0, _SRC_W, _SRC_H, 1920, 1080
    )
    assert view_yaw == pytest.approx(-10.0, abs=0.11)


def test_zoom_scale_round_trip():
    """The planner pre-applies the calibrated zoom scale: the encoded view hfov
    must equal the command's hfov regardless of render_zoom_scale."""
    for scale in (0.9, 1.0, 1.25):
        cfg = RenderStepConfig(render_zoom_scale=scale, render_auto_level=False)
        geom = _geom(cfg)
        params, _ = _frame_view(
            (3840.0, 1200.0, 52.0), geom, cfg, -90.0, 90.0, _SRC_W, _SRC_H, 1920, 1080
        )
        assert params.view_hfov_deg == pytest.approx(52.0, abs=0.11)


def test_command_for_holds_span_ends():
    commands = [[100.0, 200.0, 50.0], [110.0, 210.0, 51.0], [120.0, 220.0, 52.0]]
    assert _command_for(commands, 10, 0) == (100.0, 200.0, 50.0)  # before span
    assert _command_for(commands, 10, 11) == (110.0, 210.0, 51.0)  # inside
    assert _command_for(commands, 10, 500) == (120.0, 220.0, 52.0)  # after span


def test_load_commands_rejects_empty(tmp_path):
    p = tmp_path / "cam.json"
    p.write_text(json.dumps({"g_start": 0, "frames": []}))
    with pytest.raises(RuntimeError, match="no commands"):
        _load_commands(str(p))


def test_crop_box_never_truncates_at_extreme_pans():
    """End-field pans: the window SHIFTS to fit the pano, never truncates —
    truncation + output resize stretched the picture (2026-07-10 defect)."""
    from video_grouper.inference.cylindrical_view import crop_box
    from video_grouper.pipeline.steps.render import _command_for  # noqa: F401

    cfg = RenderStepConfig(render_zoom_scale=1.0)
    geom = _geom(cfg)
    assert geom.leveled_pano is not None
    pano = geom.leveled_pano
    ph, pw = pano.map_x.shape
    deg_px_az = (pano.az_hi - pano.az_lo) / pw
    deg_px_el = (pano.el_hi - pano.el_lo) / ph
    for cx in (0.0, 300.0, _SRC_W / 2, _SRC_W - 300.0, float(_SRC_W)):
        params, vy = _frame_view(
            (cx, 1200.0, 52.0), geom, cfg, -90.0, 90.0, _SRC_W, _SRC_H, 1920, 1080
        )
        _x0, _y0, w, h = crop_box(pano, params, vy)
        want_w = params.view_hfov_deg / deg_px_az
        want_h = (params.view_hfov_deg * 1080 / 1920) / deg_px_el
        assert w >= 0.98 * want_w, (cx, w, want_w)
        assert h >= 0.98 * want_h, (cx, h, want_h)
