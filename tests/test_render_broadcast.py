"""Render step (dumb renderer): artifact requirement + viewport telemetry."""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from video_grouper.inference.cylindrical_view import (
    pixel_to_yaw_pitch,
    yaw_pitch_to_pixel,
)
from video_grouper.pipeline.steps.render import (
    RenderStepConfig,
    _frame_view,
    _resolve_geometry,
    viewport_logger,
)

SRC_W, SRC_H = 7680, 2160
_POLY = np.array(
    [
        [200.0, 1900.0],
        [2000.0, 1950.0],
        [3840.0, 1980.0],
        [5680.0, 1950.0],
        [7480.0, 1900.0],
        [7000.0, 700.0],
        [5600.0, 650.0],
        [3840.0, 630.0],
        [2080.0, 650.0],
        [680.0, 700.0],
    ],
    dtype=np.float32,
)


def _geom(cfg: RenderStepConfig):
    return _resolve_geometry(SRC_W, SRC_H, cfg, _POLY)


def test_viewport_log_line_is_autocam_format_and_round_trips(caplog):
    """The render loop emits one JSON line per frame, AutoCam-shaped
    ({"xy":[cx,cy],"f":f,"t":t}), where xy is the source pixel the output
    frame centre maps to. Verify the format and that the point round-trips
    back to the view's (yaw, pitch)."""
    cfg = RenderStepConfig()
    geom = _geom(cfg)
    # A command on the far-right of the pano so yaw is clearly non-zero.
    params, view_yaw = _frame_view(
        (SRC_W * 0.8, SRC_H * 0.5, 52.0),
        geom,
        cfg,
        -geom.src_hfov_deg / 2,
        geom.src_hfov_deg / 2,
        SRC_W,
        SRC_H,
        cfg.render_output_width,
        cfg.render_output_height,
    )
    center_pitch = params.view_pitch_deg + params.view_pitch_offset_deg
    cx, cy = yaw_pitch_to_pixel(
        view_yaw, center_pitch, SRC_W, SRC_H, params.src_hfov_deg
    )

    # The exact line the step logs.
    with caplog.at_level(logging.INFO, logger=viewport_logger.name):
        viewport_logger.info(
            '{"xy": [%d, %d], "f": %d, "t": %.2f}', round(cx), round(cy), 1, 0.0
        )
    rec = json.loads(caplog.records[-1].getMessage())
    assert set(rec) == {"xy", "f", "t"}
    assert rec["f"] == 1 and rec["t"] == 0.0
    assert len(rec["xy"]) == 2

    # Round-trip: the logged pixel maps back to the view's yaw/pitch.
    back_yaw, back_pitch = pixel_to_yaw_pitch(
        rec["xy"][0], rec["xy"][1], SRC_W, SRC_H, params.src_hfov_deg
    )
    assert abs(back_yaw - view_yaw) < 1.0
    assert abs(back_pitch - center_pitch) < 1.0


@pytest.mark.asyncio
async def test_render_step_requires_camera_path(tmp_path):
    """No camera_path artifact in the manifest -> hard error, never a fallback."""
    from video_grouper.pipeline.manifest import PipelineManifest
    from video_grouper.pipeline.steps.render import RenderStep

    step = RenderStep(RenderStepConfig())
    m = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "in.mp4"), str(tmp_path / "out.mp4")
    )
    with pytest.raises(RuntimeError, match="plan_camera"):
        await step.run(m, None)
