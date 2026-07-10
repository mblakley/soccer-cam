"""DUMB renderer: execute a camera_path/1 artifact — warp, clamp, encode. Nothing else.

The other half of the dumb-renderer split (Mark 2026-07-09): the planner owns all
camera intelligence; this driver executes ``{center_px, hfov_deg}`` per frame and
enforces ONLY hard projection feasibility (yaw clamped to the field's lateral
extent, vertical containment / cap limits via the render branch's ``_solve_framing``,
world-up leveling). Cinematography decisions do not happen here.

Projection machinery is reused from the ``feat/broadcast-camera-render`` worktree
(``--render-worktree``) via a deliberate import order: this checkout's ``training``
package is imported FIRST (locking it in ``sys.modules``), then the worktree is
prepended to ``sys.path`` so ``video_grouper`` resolves to the render branch. The
planner already applies the AutoCam-matched zoom scale, so the render config runs
with ``render_zoom_scale=1.0`` (no double scaling).

    python -m training.cli.render_camera_path \
      --camera-path G:/ballresearch/selector/campath/spc.json \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --render-worktree G:/ballresearch/selector/repo_render \
      --start-g 36668 --end-g 36988 --out G:/ballresearch/selector/clips/spc_dyn.mp4
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera-path", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--render-worktree", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start-g", type=int, required=True)
    ap.add_argument("--end-g", type=int, required=True)
    ap.add_argument("--bitrate", default="8M")
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    # OUR checkout first: lock the training package before the worktree joins the path
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation

    sys.path.insert(0, args.render_worktree)
    import av
    import cv2
    import numpy as np

    from video_grouper.inference.cylindrical_view import (
        CylindricalViewParams,
        leveling_roll,
        pixel_to_yaw_pitch,
        yaw_pitch_to_pixel,
    )
    from video_grouper.inference.field_geometry import field_lateral_yaw_extent
    from video_grouper.pipeline.steps.render import (
        RenderStepConfig,
        _make_warper,
        _parse_bitrate,
        _resolve_geometry,
        _solve_framing,
        _warp_frame,
    )

    art = json.loads(Path(args.camera_path).read_text(encoding="utf-8"))
    g0 = int(art["g_start"])
    cmds = art["frames"]
    fps = float(art.get("fps", 20.0))
    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    polygon = np.asarray(gj["field_polygon"], np.float32)
    seg0 = gj["segments"][0]
    src_w, src_h = int(seg0["w"]), int(seg0["h"])

    cfg = RenderStepConfig(
        render_zoom_scale=1.0,  # planner pre-applied the AutoCam-matched 0.90
        render_top_cap_deg=0.0,  # strict containment: never sample past the source top
    )
    out_w, out_h = cfg.render_output_width, cfg.render_output_height
    geom = _resolve_geometry(src_w, src_h, cfg, polygon)
    try:
        warper = _make_warper(geom, cfg, src_w, src_h, out_w, out_h)
    except Exception:
        warper = None  # cv2 remap path
    yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, geom.src_hfov_deg)
    yaw_min = max(yaw_min - cfg.render_yaw_padding_deg, -geom.src_hfov_deg / 2)
    yaw_max = min(yaw_max + cfg.render_yaw_padding_deg, geom.src_hfov_deg / 2)
    vrot = resolve_video_rotation(
        str(gd / (gj.get("combined_video") or "combined.mp4")),
        gj.get("video_rotation"),
    )

    want = set(range(args.start_g, args.end_g))
    n_done = 0
    with av.open(args.out, mode="w") as out_c:
        stream = out_c.add_stream("h264", rate=int(round(fps)))
        stream.width = out_w
        stream.height = out_h
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, int(round(fps)))
        stream.codec_context.bit_rate = _parse_bitrate(args.bitrate)
        for g, img in iter_frames_from_segments(
            gd, gj["segments"], want, vrot, hwaccel=not args.no_hwaccel
        ):
            i = g - g0
            if not (0 <= i < len(cmds)):
                continue
            cx, cy, hfov = cmds[i]
            yaw, pitch = pixel_to_yaw_pitch(
                float(cx), float(cy), src_w, src_h, geom.src_hfov_deg
            )
            yaw = float(np.clip(yaw, yaw_min, yaw_max))  # feasibility clamp
            view_hfov = float(hfov)
            if geom.world_up is not None:
                ball_row = yaw_pitch_to_pixel(
                    yaw, pitch, src_w, src_h, geom.src_hfov_deg
                )[1]
                view_pitch_offset, view_hfov = _solve_framing(
                    src_w, src_h, geom, cfg, yaw, pitch, ball_row, view_hfov
                )
                view_roll = leveling_roll(
                    yaw,
                    pitch,
                    view_hfov,
                    geom.mount_tilt_deg,
                    geom.world_up,
                    out_w,
                    out_h,
                )
            else:
                view_pitch_offset = cfg.render_view_pitch_offset_deg
                view_roll = cfg.render_view_roll_deg
            params = CylindricalViewParams(
                src_w=src_w,
                src_h=src_h,
                src_hfov_deg=geom.src_hfov_deg,
                out_w=out_w,
                out_h=out_h,
                view_hfov_deg=round(view_hfov, 1),
                src_vfov_deg=-1.0,
                view_vfov_deg=-1.0,
                view_pitch_deg=round(pitch, 1),
                mount_tilt_deg=geom.mount_tilt_deg,
                view_pitch_offset_deg=round(float(view_pitch_offset), 2),
                view_roll_deg=round(float(view_roll), 2),
            )
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            rendered = _warp_frame(rgb, geom, cfg, params, round(yaw, 1), warper)
            frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
            frame.pts = n_done
            for pkt in stream.encode(frame):
                out_c.mux(pkt)
            n_done += 1
        for pkt in stream.encode():
            out_c.mux(pkt)
    if warper is not None:
        warper.close()
    print(f"rendered {n_done} frames -> {args.out}")


if __name__ == "__main__":
    main()
