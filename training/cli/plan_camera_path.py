"""Plan the camera path from the champion track and SCORE it against the benchmark.

The camera path is a first-class artifact (dumb-renderer architecture, Mark
2026-07-09): per-source-frame ``{center_px, hfov_deg}`` commands. Because it is
pure data, the viewport benchmark can grade the PLANNED camera — what viewers
will actually get — before any pixel is rendered. Two scores are reported:

- **fixed ellipse** (1200x500 source px): apples-to-apples with every track
  score published so far;
- **planned-view ellipse** (derived per frame from the command's own hfov, 16:9):
  the honest "is the ball inside the frame we intend to render".

    python -m training.cli.plan_camera_path \
      --net G:/ballresearch/selector/selector_v5.pt \
      --fullgame-dir .../fullgame_heldout/heat__2026.05.31_vs_Spencerport_gold_2_away \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --out G:/ballresearch/selector/spc_camera_path.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np


def depth_from_polygon(
    traj: list[tuple[float, float] | None], polygon: np.ndarray
) -> list[float | None]:
    """Field depth per frame (0 = far touchline, 1 = near) from the image y of the
    ball between the polygon's far (points 5-9) and near (0-4) line means."""
    y_near = float(np.mean(polygon[0:5, 1]))
    y_far = float(np.mean(polygon[5:10, 1])) if len(polygon) >= 10 else y_near - 1.0
    span = max(y_near - y_far, 1e-6)
    out: list[float | None] = []
    for p in traj:
        if p is None:
            out.append(None)
        else:
            out.append(float(np.clip((p[1] - y_far) / span, 0.0, 1.0)))
    return out


def score_plan(
    plan: list[tuple[float, float, float]],
    g_start: int,
    bench: dict[int, dict],
    src_w: int,
    *,
    traj: list | None = None,
    fixed: tuple[float, float] | None = (1200.0, 500.0),
) -> dict:
    """Ball-in-planned-view rates by benchmark tier + sustained loss windows.

    Rows where the planner had NO input (``traj[i] is None`` — outside active
    play / track coverage) are excluded and counted as ``uncovered``: the render
    is phase-gated there, so the camera cannot be graded on them."""
    tally = {"human": [0, 0], "autocam": [0, 0]}
    events: list[tuple[int, bool]] = []
    uncovered = 0
    for g, r in sorted(bench.items()):
        i = g - g_start
        if not (0 <= i < len(plan)):
            continue
        if traj is not None and traj[i] is None:
            uncovered += 1
            continue
        cx, cy, hfov = plan[i]
        if fixed is not None:
            hw, hh = fixed
        else:
            hw = src_w * (hfov / 180.0) / 2.0
            hh = hw * (1080.0 / 1920.0)
        inside = ((r["x"] - cx) / hw) ** 2 + ((r["y"] - cy) / hh) ** 2 <= 1.0
        tally[r["tier"]][1] += 1
        tally[r["tier"]][0] += int(inside)
        events.append((g, inside))
    runs: list[list] = []
    for g, ok in events:
        if runs and runs[-1][0] == ok and g - runs[-1][2] <= 48:
            runs[-1][2] = g
        else:
            runs.append([ok, g, g])
    loss = [(r[1], r[2]) for r in runs if not r[0] and (r[2] - r[1]) >= 40]
    return {
        "human": tally["human"],
        "autocam": tally["autocam"],
        "loss": loss,
        "uncovered": uncovered,
    }


def replay_champion_chain(
    fullgame_dir: Path | str,
    game_dir: Path | str,
    net_path: str,
    *,
    emission_weight: float = 1.0,
    pnone_scale: float = 1.0,
    pnone_far_scale: float = 1.0,
    pnone_far_diam: float = 8.0,
    pnone_far_near_diam: float = 0.0,
    pnone_depr_far_deg: float = 0.0,
    pnone_depr_near_deg: float = 0.0,
    world_model: str = "homography",
    vmax_scale: float = 1.0,
    phys_sigma_px: float = 5.0,
    bridge_w: float = 2.0,
    oob_w: float = 2.0,
    static_w: float = 2.0,
    stride: int = 1,
    planner_config=None,
    static_filter_frac: float | None = None,
    static_filter_cell: float = 50.0,
    static_filter_radius: float = 60.0,
    static_filter_offfield_only: bool = False,
    static_filter_field_margin_m: float = 2.0,
    miss_entry_near_k: float = 0.0,
    miss_entry_margin_k: float = 0.0,
) -> dict:
    """The CURRENT champion camera chain, exactly as this CLI runs it: cached
    ``fullgame_candidates/1`` dump -> selector emissions -> rerank (shipped
    config) -> aerial bridge -> Kalman smooth -> upsample -> depth ->
    :func:`plan_camera`.

    Extracted UNCHANGED from ``main`` (2026-07-25) so the oracle ladder
    (``training.cli.operator_ladder`` run-a) replays the same code instead of
    forking it; the CLI behavior is identical. ``stride`` (ladder-only, default
    1 = CLI behavior) subsamples the candidate grid before the chain.
    ``planner_config`` (ladder-only, default None = pipeline ``PlannerConfig``)
    reaches :func:`plan_camera`. The trajectory/2 channels (states/conf/disp)
    are always computed, returned, and handed to the planner — with the default
    config (``enable_hold=False``) the planner ignores them, so CLI behavior is
    unchanged.

    Returns ``{"ef", "sel", "track", "traj", "states", "conf", "disp",
    "depth01", "plan", "g_start", "src_w", "src_h", "fps", "gj", "polygon"}``.
    """
    from training.cli.build_selector_labels import load_fullgame_candidates
    from training.models.selector_net import load_selector, pack_frames, predict_probs
    from training.world_model.camera_planner import (
        plan_camera,
        upsample_disp,
        upsample_track,
    )
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import (
        RerankConfig,
        bridge_aerial_gaps,
        candidate_dispersion,
        kalman_smooth,
        rerank,
        static_candidate_filter,
    )
    from training.world_model.selector_features import build_features
    from training.world_model.tbd import Candidate

    gd = Path(game_dir)
    ef, cands, _meta = load_fullgame_candidates(Path(fullgame_dir))
    if stride > 1:
        ef = ef[::stride]
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    polygon = np.asarray(gj["field_polygon"], float)
    geom = build_field_geometry(polygon)
    static_centers: list[tuple[float, float]] = []
    if static_filter_frac:
        cands, static_centers = static_candidate_filter(
            ef,
            cands,
            cell_px=static_filter_cell,
            presence_frac=static_filter_frac,
            radius_px=static_filter_radius,
        )
        if static_filter_offfield_only and static_centers:
            # EXP-OP-11 F2: an OFF-FIELD static is never the ball in play; an
            # IN-FIELD static may be the keeper holding it -- leave those to the
            # world-cell static_w penalty. Re-drop with only off-field centers.
            from video_grouper.inference.world_geometry import (
                DEFAULT_FIELD_LENGTH_M,
                DEFAULT_FIELD_WIDTH_M,
            )

            m = static_filter_field_margin_m
            wc = geom.image_to_world(np.asarray(static_centers, float))
            off = [
                c
                for c, (wx, wy) in zip(static_centers, wc, strict=False)
                if not (
                    -m <= wx <= DEFAULT_FIELD_LENGTH_M + m
                    and -m <= wy <= DEFAULT_FIELD_WIDTH_M + m
                )
            ]
            ef2, cands2, _ = load_fullgame_candidates(Path(fullgame_dir))
            if stride > 1:
                ef2 = ef2[::stride]
            if off:
                r2 = static_filter_radius**2
                cands = {
                    g: [
                        row
                        for row in cands2[g]
                        if not any(
                            (row[0] - a) ** 2 + (row[1] - b) ** 2 <= r2 for a, b in off
                        )
                    ]
                    for g in ef2
                }
            else:
                cands = cands2
            static_centers = off
    if len(ef) < 2:
        raise SystemExit(
            f"plan_camera_path: candidate dump {fullgame_dir} has {len(ef)} "
            "frames after stride -- need >= 2"
        )
    seg0 = gj["segments"][0]
    src_w, src_h = int(seg0["w"]), int(seg0["h"])
    frames = [
        [Candidate(x=x, y=y, score=s, size_px=None) for (x, y, s, _z) in cands[g]]
        for g in ef
    ]
    gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]
    net, keep = load_selector(net_path)
    feats = [x[:, keep] for x in build_features(frames, geom, ef=ef)]
    packed, mask = pack_frames(feats)
    probs = predict_probs(net, packed, mask)
    w = emission_weight
    priors = [
        w * -np.log(np.maximum(probs[i, : len(fr)], 1e-6)) if fr else np.zeros(0)
        for i, fr in enumerate(frames)
    ]
    # miss cost = pnone_scale * -log(p_none). The hold on a detected far ball is
    # DEPTH-CONDITIONED via the per-game homography (expected ball diameter).
    # - BINARY gate (fd6, EXP-OP-25/29): full pnone_far_scale when diam <
    #   pnone_far_diam, else pnone_scale. A global switch — good far, small near
    #   cost at the far→near boundary.
    # - CONTINUOUS ramp (EXP-OP-30, the geometry-conditioned successor, Mark
    #   2026-07-29): when pnone_far_near_diam > pnone_far_diam, the strength
    #   ramps SMOOTHLY from pnone_scale at near_diam to pnone_far_scale at
    #   far_diam — a function of depth, not a switch, to dissolve the near/far
    #   tension. (Leans harder on the homography, the ONE BUG CLASS ruler — see
    #   DECISIONS (y); prototype on current geometry, durable form gated on #19.)
    # - DEPRESSION-CONDITIONED (#19, EXP-OP-33, Mark's lens-compensated axis):
    #   condition on the ray-geometry DEPRESSION ANGLE below the leveled horizon
    #   (from the polygon-derived world-up, cylindrical_view.field_world_up)
    #   instead of the planar expected diameter. Depression is the lens-CORRECT
    #   farness measure — the planar ruler bows ±35%, worst at edges/far
    #   (EXP-OP-32) — and unlike range it stays bounded at the horizon. Full
    #   pnone_far_scale at depr<=pnone_depr_far_deg (far), ramping to pnone_scale
    #   at depr>=pnone_depr_near_deg (near). Active when near_deg>far_deg;
    #   supersedes the diam path. This is the durable form (y) was gated on.
    depr_cond = pnone_depr_near_deg > pnone_depr_far_deg
    r_cw = None
    if depr_cond:
        from video_grouper.inference.cylindrical_view import (
            pixel_depression_deg,
            polygon_leveling_rotation,
        )

        r_cw = polygon_leveling_rotation(polygon, src_w, src_h, 180.0)
    depr_span = max(pnone_depr_near_deg - pnone_depr_far_deg, 1e-6)

    ramp = pnone_far_near_diam > pnone_far_diam
    span = max(pnone_far_near_diam - pnone_far_diam, 1e-6)
    mc = []
    for i in range(len(frames)):
        base = w * -np.log(max(float(probs[i, -1]), 1e-6))
        scale = pnone_scale
        if pnone_far_scale != pnone_scale and frames[i]:
            jt = int(np.argmax(probs[i, : len(frames[i])]))
            top = frames[i][jt]
            if r_cw is not None:
                depr = pixel_depression_deg(top.x, top.y, r_cw, src_w, src_h, 180.0)
                wf = min(max((pnone_depr_near_deg - depr) / depr_span, 0.0), 1.0)
                scale = pnone_scale + (pnone_far_scale - pnone_scale) * wf
            else:
                diam = float(
                    geom.expected_ball_diameter_px(np.asarray([[top.x, top.y]], float))[
                        0
                    ]
                )
                if ramp:
                    wf = min(max((pnone_far_near_diam - diam) / span, 0.0), 1.0)
                    scale = pnone_scale + (pnone_far_scale - pnone_scale) * wf
                elif diam < pnone_far_diam:
                    scale = pnone_far_scale
        mc.append(float(scale * base))
    cfg = replace(
        RerankConfig(),
        alpha=0.0,
        static_w=static_w,
        motion_w=0.0,
        phys_sigma_px=phys_sigma_px,
        bridge_w=bridge_w,
        oob_w=oob_w,
        # W3 stage-1 (task #22): state-dependent miss-ENTRY cost arms; both
        # default 0.0 = the shipped chain, bit-identical
        miss_entry_near_k=miss_entry_near_k,
        miss_entry_margin_k=miss_entry_margin_k,
    )
    if vmax_scale != 1.0:
        # EXP-OP-34 refit: the meter-based speed gates were implicitly tuned to
        # the homography's COMPRESSED far meters (over-sized far, EXP-OP-32);
        # under the ray metric the same gates reject legitimate far motion.
        cfg = replace(
            cfg,
            ball_vmax_mpf=cfg.ball_vmax_mpf * vmax_scale,
            air_vmax_mpf=cfg.air_vmax_mpf * vmax_scale,
        )
    # EXP-OP-34 (#19 ray world model): the TRACKER's meters (physics vmax gate,
    # Jacobian measurement noise, world Kalman, oob/restart/bridge) come from
    # tracker_geom. world_model="ray" swaps in the ray-ground-intersection
    # geometry (correct meters; the planar homography bows +/-35%, EXP-OP-32)
    # while the SELECTOR features stay on the trained planar geom above.
    tracker_geom = geom
    if world_model == "ray":
        from video_grouper.inference.world_geometry import build_ray_field_geometry

        ray_geom = build_ray_field_geometry(polygon, src_w, src_h, 180.0)
        if ray_geom is None:
            raise SystemExit(
                "plan_camera_path: world_model=ray but the polygon cannot "
                "support a ray geometry (degenerate/mis-ordered/no world-up)"
            )
        tracker_geom = ray_geom
    elif world_model != "homography":
        raise SystemExit(f"plan_camera_path: unknown world_model {world_model!r}")
    picked, pick_conf = rerank(
        frames,
        tracker_geom,
        frame_gaps=gaps,
        priors=priors,
        miss_costs=mc,
        config=cfg,
        return_states=True,
    )
    sel = bridge_aerial_gaps(picked, tracker_geom, frame_gaps=gaps, config=cfg)
    track = kalman_smooth(sel, tracker_geom)
    # trajectory/2 grid channels: 'T' = a real rerank-selected candidate on the
    # Viterbi path; Kalman coast fills / bridge interpolations are 'C'.
    g_states = {i: ("T" if i in picked else "C") for i in track}
    g_conf = {i: (pick_conf[i] if i in picked else 0.0) for i in track}

    g_start, g_end = int(ef[0]), int(ef[-1]) + 1
    traj, states, conf = upsample_track(
        track, ef, g_start, g_end, states=g_states, conf=g_conf
    )
    disp = upsample_disp(candidate_dispersion(frames), ef, g_start, g_end, points=traj)
    depth01 = depth_from_polygon(traj, polygon)
    plan = plan_camera(
        traj,
        src_w=src_w,
        src_h=src_h,
        depth01=depth01,
        states=states,
        disp=disp,
        config=planner_config,
    )
    return {
        "ef": ef,
        "sel": sel,
        "track": track,
        "traj": traj,
        "states": states,
        "conf": conf,
        "disp": disp,
        "depth01": depth01,
        "plan": plan,
        "g_start": g_start,
        "src_w": src_w,
        "src_h": src_h,
        "fps": float(gj.get("fps", 20.0)),
        "gj": gj,
        "polygon": polygon,
        "static_centers": static_centers,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True)
    ap.add_argument("--fullgame-dir", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True, help="camera_path/1 artifact path")
    ap.add_argument("--emission-weight", type=float, default=1.0)
    ap.add_argument("--pnone-scale", type=float, default=1.0)
    ap.add_argument("--phys-sigma-px", type=float, default=5.0)
    ap.add_argument("--bridge-w", type=float, default=2.0)
    ap.add_argument("--oob-w", type=float, default=2.0)
    ap.add_argument("--static-w", type=float, default=2.0)
    args = ap.parse_args()

    from training.world_model.camera_planner import save_camera_path

    gd = Path(args.game_dir)
    res = replay_champion_chain(
        args.fullgame_dir,
        args.game_dir,
        args.net,
        emission_weight=args.emission_weight,
        pnone_scale=args.pnone_scale,
        phys_sigma_px=args.phys_sigma_px,
        bridge_w=args.bridge_w,
        oob_w=args.oob_w,
        static_w=args.static_w,
    )
    ef, sel, traj, plan = res["ef"], res["sel"], res["traj"], res["plan"]
    g_start, src_w = res["g_start"], res["src_w"]
    save_camera_path(
        args.out,
        plan,
        g_start=g_start,
        src_w=src_w,
        src_h=res["src_h"],
        fps=res["fps"],
    )
    # Debug sidecar for the eval renderer. Two distinct signals:
    #  - "detections": the RAW per-frame SELECTED ball (rerank's chosen candidate at
    #    the detector-peak pixel), keyed by GLOBAL frame — where the ball was actually
    #    found (stride-spaced). This is what the debug ball marker draws.
    #  - "frames": the smoothed + upsampled dense track (what the camera path follows).
    # Not a product artifact.
    detections = {
        str(int(ef[i])): [round(float(x), 1), round(float(y), 1)]
        for i, (x, y) in sel.items()
        if 0 <= i < len(ef)
    }
    track_path = Path(args.out).with_suffix(".track.json")
    track_path.write_text(
        json.dumps(
            {
                "g_start": g_start,
                "detections": detections,
                "frames": [
                    [round(float(p[0]), 1), round(float(p[1]), 1)] if p else None
                    for p in traj
                ],
            }
        )
    )
    print(f"{gd.name}: camera path {len(plan)} frames -> {args.out}")

    bench_path = gd / "viewport_benchmark.jsonl"
    if not bench_path.exists():
        print("no viewport_benchmark.jsonl — skipping score")
        return
    bench: dict[int, dict] = {}
    for ln in bench_path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("tier") != "none":
                bench[int(r["g"])] = r
    for label, fixed in (("fixed 1200x500", (1200.0, 500.0)), ("planned-view", None)):
        s = score_plan(plan, g_start, bench, src_w, traj=traj, fixed=fixed)
        bh, ba = s["human"], s["autocam"]
        print(
            f"  {label:14s} human {bh[0]}/{bh[1]} = {bh[0] / max(bh[1], 1):.3f}  "
            f"autocam-tier {ba[0]}/{ba[1]} = {ba[0] / max(ba[1], 1):.3f}  "
            f"loss-windows {len(s['loss'])}  uncovered {s['uncovered']}"
        )


if __name__ == "__main__":
    main()
