"""Camera-planning step — trajectory in, explicit per-frame camera path out.

The dumb-renderer split (2026-07-09): ALL camera intelligence lives upstream in
:mod:`video_grouper.inference.camera_planner` (AutoCam-calibrated aesthetics,
angular units, dead-ball/hold behavior); the render step then EXECUTES the
resulting ``camera_path/1`` artifact and enforces only projection feasibility.
This step is the production home of that planning pass:

    detect -> track (trajectory.json) -> plan_camera (camera_path.json) -> render
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import cast

import numpy as np
from pydantic import BaseModel

from video_grouper.inference.camera_planner import (
    PlannerConfig,
    plan_camera,
    save_camera_path,
)
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class PlanCameraStepConfig(BaseModel):
    # Aesthetic tunables forwarded to the planner; defaults are the
    # AutoCam-calibrated values (see PlannerConfig).
    plan_zoom_scale: float = 0.90
    plan_lead_frames: float = 8.0
    plan_deadball_hfov_deg: float = 52.0
    plan_missing_hfov_deg: float = 58.0


def _depth01(trajectory: list, polygon: np.ndarray | None) -> list[float | None] | None:
    """Ball field-depth per frame (0 = far line, 1 = near) for the zoom curve."""
    if polygon is None or len(polygon) < 10:
        return None
    y_near = float(np.mean(polygon[0:5, 1]))
    y_far = float(np.mean(polygon[5:10, 1]))
    span = max(y_near - y_far, 1e-6)
    out: list[float | None] = []
    for p in trajectory:
        if p is None:
            out.append(None)
        else:
            out.append(float(np.clip((float(p[1]) - y_far) / span, 0.0, 1.0)))
    return out


def _plan(
    trajectory_path: str,
    polygon_path: str | None,
    out_path: str,
    src_w: int,
    src_h: int,
    fps: float,
    cfg: PlanCameraStepConfig,
) -> int:
    with open(trajectory_path, encoding="utf-8") as f:
        trajectory = json.load(f)
    polygon = None
    if polygon_path:
        try:
            with open(polygon_path, encoding="utf-8") as f:
                payload = json.load(f)
            poly = payload.get("polygon")
            polygon = np.asarray(poly, float) if poly is not None else None
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning("plan_camera: polygon %s unusable (%s)", polygon_path, e)
    traj = [tuple(p) if p is not None else None for p in trajectory]
    plan = plan_camera(
        traj,
        src_w=src_w,
        src_h=src_h,
        depth01=_depth01(traj, polygon),
        config=PlannerConfig(
            zoom_scale=cfg.plan_zoom_scale,
            lead_frames=cfg.plan_lead_frames,
            deadball_hfov_deg=cfg.plan_deadball_hfov_deg,
            missing_hfov_deg=cfg.plan_missing_hfov_deg,
            fps=fps,
        ),
    )
    save_camera_path(out_path, plan, g_start=0, src_w=src_w, src_h=src_h, fps=fps)
    return len(plan)


class PlanCameraStep(PipelineStep[PlanCameraStepConfig]):
    name = "plan_camera"
    config_model = PlanCameraStepConfig
    consumes = ("trajectory_path",)
    produces = ("camera_path_path",)
    runtime = "service"
    requires = ()
    resources = ()

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        trajectory_path = cast(str, manifest.get("trajectory_path"))
        in_path = Path(cast(str, manifest.get("input_path")))
        out_path = in_path.with_name("camera_path.json")

        import av

        with av.open(str(in_path)) as probe:
            vs = probe.streams.video[0]
            src_w, src_h = vs.codec_context.width, vs.codec_context.height
            fps = float(vs.average_rate) if vs.average_rate else 20.0

        n = await asyncio.to_thread(
            _plan,
            trajectory_path,
            manifest.get("field_polygon_path"),
            str(out_path),
            src_w,
            src_h,
            fps,
            self.config,
        )
        logger.info("plan_camera: %d commands -> %s", n, out_path)
        manifest.put("camera_path_path", str(out_path))
        return True


register_step(PlanCameraStep.name, PlanCameraStep, PlanCameraStepConfig)
