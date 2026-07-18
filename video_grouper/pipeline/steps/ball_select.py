"""Selection step — pick the game ball from the detector's candidates, per frame.

The champion selection stack (validated on held-out games against the viewport
benchmark): context features -> the learned listwise selector (calibrated
P(candidate) / P(none visible)) -> the physics Viterbi
(:func:`video_grouper.inference.ball_tracker.rerank`: static-persistence,
depth-aware measurement noise, aerial bridge, out-of-bounds pin) -> the
constant-velocity Kalman RTS smoother -> dense per-frame upsampling.

Reads the ``ball_detect`` step's candidates artifact (``candidates/2`` rows are
``(x, y, score, size_px)``; legacy ``candidates/1`` 3-tuples still accepted) +
the field polygon, writes ``trajectory.json`` (one ``[x, y]`` row per source
frame, ``null`` when the ball has no estimate — the same contract
``plan_camera`` consumes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import cast

import numpy as np
from pydantic import BaseModel

from video_grouper.inference.ball_selector import (
    build_features,
    load_selector,
    pack_frames,
    predict_probs,
)
from video_grouper.inference.ball_tracker import (
    Candidate,
    RerankConfig,
    bridge_aerial_gaps,
    kalman_smooth,
    rerank,
)
from video_grouper.inference.camera_planner import upsample_track
from video_grouper.inference.world_geometry import build_field_geometry
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class BallSelectStepConfig(BaseModel):
    # The exported listwise selector (selector_net_npz/1). The learned emission is
    # part of the champion stack — a preset leaves this unset for the user to
    # supply (like the detector model source), and running without one is a
    # hard error, never an unlearned fallback.
    select_model_path: str | None = None
    # Champion replay config (held-out validated): learned emission + static
    # hybrid, physics transitions, aerial bridge, out-of-bounds pin.
    select_emission_weight: float = 1.0
    select_pnone_scale: float = 1.0
    select_static_w: float = 2.0
    select_phys_sigma_px: float = 5.0
    select_bridge_w: float = 2.0
    select_oob_w: float = 2.0  # EXP-DIST-48: oob0 helped raw-selected but planned-viewport-vs-GT favors 2.0
    # Minimum candidate-pack width. The step auto-expands this to fit the actual
    # per-frame candidate count so it can never truncate candidates; it does NOT
    # affect feature normalization (that is pinned to the training top_k inside
    # build_features).
    select_top_k: int = 24
    # Dense-trajectory upsampling: interpolate between selected samples, but blank
    # gaps longer than this many source frames (play discontinuities).
    select_max_gap_frames: int = 24


def _rows_to_candidates(rows: list) -> list[Candidate]:
    """Artifact rows -> Candidates. candidates/2 rows are (x, y, score, size_px);
    legacy candidates/1 rows are (x, y, score) -> size_px stays None (the
    tracker's size-continuity term simply stays dormant for those artifacts)."""
    out = []
    for row in rows:
        x, y, s = row[0], row[1], row[2]
        sz = float(row[3]) if len(row) > 3 and row[3] else None
        out.append(Candidate(x=float(x), y=float(y), score=float(s), size_px=sz))
    return out


def _run_selection(
    detections_path: str,
    polygon_path: str,
    output_json_path: str,
    cfg: BallSelectStepConfig,
) -> int:
    with open(detections_path, encoding="utf-8") as f:
        art = json.load(f)
    if art.get("schema") != "candidates/1":
        raise RuntimeError(
            f"select: {detections_path} is not a candidates/1 artifact "
            f"(got {art.get('schema')!r}) — re-run ball_detect."
        )
    with open(polygon_path, encoding="utf-8") as f:
        polygon = np.asarray(json.load(f)["polygon"], float)
    geom = build_field_geometry(polygon)
    if not geom.valid:
        raise RuntimeError(
            "select: could not fit a field-plane homography from the polygon — "
            "selection runs in world meters and requires a valid 10-point field "
            "outline (fix field_detect's output)."
        )

    by_g = {int(g): rows for g, rows in art["frames"].items()}
    ef = sorted(by_g)
    if not ef:
        raise RuntimeError("select: candidates artifact has no frames")
    frames = [_rows_to_candidates(by_g[g]) for g in ef]
    gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]

    net = load_selector(cfg.select_model_path)
    feats = [x[:, net.keep] for x in build_features(frames, geom, ef=ef)]
    # The pack width must fit EVERY frame's candidate count. If select_top_k is
    # below it, pack_frames silently TRUNCATES candidates while the priors below
    # are sliced by the full candidate count (len(fr)) -> a priors row shorter
    # than the frame's candidates, which misaligns / overruns the emission in
    # rerank. build_features' feature normalization is pinned to the TRAINING
    # top_k (its own default) and is deliberately independent of this packing
    # width, so expanding to fit here cannot perturb the learned features.
    pack_k = max(cfg.select_top_k, max((len(x) for x in feats), default=1))
    packed, mask = pack_frames(feats, top_k=pack_k)
    probs = predict_probs(net, packed, mask)
    w = cfg.select_emission_weight
    priors = [
        w * -np.log(np.maximum(probs[i, : len(fr)], 1e-6)) if fr else np.zeros(0)
        for i, fr in enumerate(frames)
    ]
    miss_costs = [
        float(cfg.select_pnone_scale * w * -np.log(max(float(probs[i, -1]), 1e-6)))
        for i in range(len(frames))
    ]
    rr_cfg = replace(
        RerankConfig(),
        alpha=0.0,
        static_w=cfg.select_static_w,
        motion_w=0.0,
        phys_sigma_px=cfg.select_phys_sigma_px,
        bridge_w=cfg.select_bridge_w,
        oob_w=cfg.select_oob_w,
    )
    sel = rerank(
        frames,
        geom,
        frame_gaps=gaps,
        priors=priors,
        miss_costs=miss_costs,
        config=rr_cfg,
    )
    sel = bridge_aerial_gaps(sel, geom, frame_gaps=gaps, config=rr_cfg)
    track = kalman_smooth(sel, geom)

    # Dense per-source-frame trajectory from frame 0 (the plan_camera contract).
    g_end = int(ef[-1]) + 1
    traj = upsample_track(track, ef, 0, g_end, max_gap=cfg.select_max_gap_frames)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(traj, f)
    return sum(1 for p in traj if p is not None)


class BallSelectStep(PipelineStep[BallSelectStepConfig]):
    name = "ball_select"
    config_model = BallSelectStepConfig
    consumes = ("detections_path",)
    produces = ("trajectory_path",)
    runtime = "service"
    requires = ("cv2",)
    resources = ()

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        if not self.config.select_model_path:
            raise RuntimeError(
                "select: select_model_path is not configured. Export the trained "
                "selector (training.cli.export_ball_selector) and point "
                "select_model_path at the .npz."
            )
        detections_path = cast(str, manifest.get("detections_path"))
        in_path = Path(cast(str, manifest.get("input_path")))
        trajectory_path = in_path.with_name("trajectory.json")
        polygon_path = manifest.get("field_polygon_path")
        if not polygon_path:
            raise RuntimeError(
                "select: field_polygon_path missing from the manifest — the "
                "selection physics run in world meters and require the "
                "field_detect step's polygon."
            )

        populated = await asyncio.to_thread(
            _run_selection,
            detections_path,
            cast(str, polygon_path),
            str(trajectory_path),
            self.config,
        )
        logger.info(
            "select: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        manifest.put("trajectory_path", str(trajectory_path))
        return True


register_step(BallSelectStep.name, BallSelectStep, BallSelectStepConfig)
