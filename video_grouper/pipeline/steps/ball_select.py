"""Selection step — pick the game ball from the detector's candidates, per frame.

The champion selection stack (validated on held-out games against the viewport
benchmark): context features -> the learned listwise selector (calibrated
P(candidate) / P(none visible)) -> the physics Viterbi
(:func:`video_grouper.inference.ball_tracker.rerank`: static-persistence,
depth-aware measurement noise, aerial bridge, out-of-bounds pin) -> the
constant-velocity Kalman RTS smoother -> dense per-frame upsampling.

Reads the ``ball_detect`` step's candidates artifact (``candidates/2`` rows are
``(x, y, score, size_px)``; legacy ``candidates/1`` 3-tuples still accepted) +
the field polygon, writes ``trajectory.json`` as a ``trajectory/2`` artifact:
per-source-frame ``points`` (``[x, y]`` or ``null``, unchanged from the
legacy bare list) plus the W2 seam channels — ``state`` (``'T'`` tracked /
``'C'`` coasted fill / ``'M'`` missing), ``conf`` (emission-derived track
confidence) and ``disp`` (per-frame candidate-cloud dispersion, px). The
``plan_camera`` step consumes this and still accepts the legacy formats.
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
    candidate_dispersion,
    kalman_smooth,
    rerank,
)
from video_grouper.inference.camera_planner import upsample_disp, upsample_track
from video_grouper.inference.cylindrical_view import (
    pixel_depression_deg,
    polygon_leveling_rotation,
)
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
    # dcB — the depression-conditioned far-hold (EXP-OP-32/33, adopted by Mark
    # 2026-07-29): when the frame's top candidate is far (small depression angle
    # below the leveled horizon, from the polygon-derived camera orientation),
    # the tracker holds it longer before taking a miss. Full select_pnone_far_
    # scale at depr <= far_deg, ramping linearly to select_pnone_scale at
    # depr >= near_deg. Depression is the lens-correct farness axis (bounded at
    # the horizon, height-independent); the planar expected-size ruler bows
    # ±35%. Disable by setting near_deg <= far_deg (flat select_pnone_scale).
    select_pnone_far_scale: float = 2.0
    select_pnone_depr_far_deg: float = 7.0
    select_pnone_depr_near_deg: float = 16.0
    select_src_hfov_deg: float = 180.0  # stitched pano span (= render's default)
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
    if art.get("schema") not in ("candidates/1", "candidates/2"):
        raise RuntimeError(
            f"select: {detections_path} is not a candidates/1|2 artifact "
            f"(got {art.get('schema')!r}) — re-run ball_detect."
        )
    fps = art.get("fps")
    if fps is None:  # fail fast: trajectory/2 requires it, don't select first
        raise RuntimeError(
            f"select: {detections_path} carries no fps (trajectory/2 requires "
            "it) — re-run ball_detect."
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
    # Miss cost = pnone_scale * -log(p_none), depression-conditioned (dcB):
    # a detected FAR ball (small depression angle) gets a stronger hold before
    # the tracker takes a miss. See BallSelectStepConfig.select_pnone_far_scale.
    depr_hold = (
        cfg.select_pnone_depr_near_deg > cfg.select_pnone_depr_far_deg
        and cfg.select_pnone_far_scale != cfg.select_pnone_scale
    )
    r_cw = None
    if depr_hold:
        src_w, src_h = art.get("src_w"), art.get("src_h")
        if not src_w or not src_h:
            raise RuntimeError(
                "select: the depression-conditioned far-hold needs src_w/src_h "
                "in the candidates artifact — re-run ball_detect (candidates/2) "
                "or disable the hold (select_pnone_depr_near_deg <= far_deg)."
            )
        r_cw = polygon_leveling_rotation(
            polygon, int(src_w), int(src_h), cfg.select_src_hfov_deg
        )
        if r_cw is None:
            raise RuntimeError(
                "select: could not derive world-up from the field polygon for "
                "the depression-conditioned far-hold — fix field_detect's "
                "output or disable the hold (select_pnone_depr_near_deg <= "
                "far_deg)."
            )
    depr_span = cfg.select_pnone_depr_near_deg - cfg.select_pnone_depr_far_deg
    miss_costs = []
    for i in range(len(frames)):
        base = w * -np.log(max(float(probs[i, -1]), 1e-6))
        scale = cfg.select_pnone_scale
        if r_cw is not None and frames[i]:
            jt = int(np.argmax(probs[i, : len(frames[i])]))
            top = frames[i][jt]
            depr = pixel_depression_deg(
                top.x, top.y, r_cw, int(src_w), int(src_h), cfg.select_src_hfov_deg
            )
            wf = min(max((cfg.select_pnone_depr_near_deg - depr) / depr_span, 0.0), 1.0)
            scale = (
                cfg.select_pnone_scale
                + (cfg.select_pnone_far_scale - cfg.select_pnone_scale) * wf
            )
        miss_costs.append(float(scale * base))
    rr_cfg = replace(
        RerankConfig(),
        alpha=0.0,
        static_w=cfg.select_static_w,
        motion_w=0.0,
        phys_sigma_px=cfg.select_phys_sigma_px,
        bridge_w=cfg.select_bridge_w,
        oob_w=cfg.select_oob_w,
    )
    sel, sel_conf = rerank(
        frames,
        geom,
        frame_gaps=gaps,
        priors=priors,
        miss_costs=miss_costs,
        config=rr_cfg,
        return_states=True,
    )
    filled = bridge_aerial_gaps(sel, geom, frame_gaps=gaps, config=rr_cfg)
    track = kalman_smooth(filled, geom)
    # trajectory/2 grid channels: 'T' = a real selected candidate on the Viterbi
    # path; Kalman coast fills (and aerial-bridge interpolations, when enabled)
    # are 'C' — interpolations are not detections.
    g_states = {i: ("T" if i in sel else "C") for i in track}
    g_conf = {i: (sel_conf[i] if i in sel else 0.0) for i in track}

    # Dense per-source-frame trajectory from frame 0 (the plan_camera contract).
    g_end = int(ef[-1]) + 1
    traj, state, conf = upsample_track(
        track,
        ef,
        0,
        g_end,
        max_gap=cfg.select_max_gap_frames,
        states=g_states,
        conf=g_conf,
    )
    disp = upsample_disp(candidate_dispersion(frames), ef, 0, g_end, points=traj)
    payload = {
        "schema": "trajectory/2",
        "g_start": 0,
        "fps": float(fps),
        # points stay exactly the legacy per-frame values (same chain, same floats)
        "points": [None if p is None else [p[0], p[1]] for p in traj],
        "state": state,
        "conf": [round(float(c), 4) for c in conf],
        "disp": [None if v is None else round(float(v), 1) for v in disp],
    }
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
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
