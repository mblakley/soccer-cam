"""Render a camera_path/1 artifact through the PRODUCTION render step code.

Thin evaluation wrapper around :mod:`video_grouper.pipeline.steps.render` — the
same module the shipped pipeline runs (dumb-renderer camera-path mode, Mark
2026-07-10: what we review must be what we ship). Two access modes share every
line of render math (``_frame_view`` + ``_warp_frame``):

- **full video** (no window): delegates to the production ``_render_video`` with
  ``camera_path_file`` — byte-identical to the pipeline's render step output.
- **clip window** (``--start-g/--end-g``): decodes exact global-frame ranges from
  the RAW segments (frame-exact, corruption-isolated) and executes the same
  per-frame solve/warp — for fast iteration on adjudicated windows.

    python -m training.cli.render_camera_path \
      --camera-path .../campath/spc.json \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --start-g 36668 --end-g 36988 --out clips/spc_dyn.mp4
"""

from __future__ import annotations

import argparse
import json
import time
from fractions import Fraction
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera-path", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start-g", type=int, default=None)
    ap.add_argument("--end-g", type=int, default=None)
    ap.add_argument("--bitrate", default="8M")
    ap.add_argument("--codec", default="h264")
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    import av
    import cv2
    import numpy as np

    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation
    from video_grouper.inference.field_geometry import field_lateral_yaw_extent
    from video_grouper.pipeline.steps.render import (
        RenderStepConfig,
        _frame_view,
        _make_warper,
        _parse_bitrate,
        _render_video,
        _resolve_geometry,
        _warp_frame,
    )

    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    cfg = RenderStepConfig(render_zoom_scale=1.0)  # planner pre-applies the scale

    if args.start_g is None:  # full video: the production path, verbatim
        field_file = Path(args.out).with_suffix(".field.json")
        field_file.write_text(json.dumps({"polygon": gj["field_polygon"]}))
        src = gd / (gj.get("combined_video") or "combined.mp4")
        _render_video(str(src), args.out, args.camera_path, str(field_file), cfg)
        print(f"rendered full video -> {args.out}")
        return

    art = json.loads(Path(args.camera_path).read_text(encoding="utf-8"))
    g0 = int(art["g_start"])
    cmds = art["frames"]
    fps = float(art.get("fps", 20.0))
    polygon = np.asarray(gj["field_polygon"], np.float32)
    seg0 = gj["segments"][0]
    src_w, src_h = int(seg0["w"]), int(seg0["h"])
    out_w, out_h = cfg.render_output_width, cfg.render_output_height
    geom = _resolve_geometry(src_w, src_h, cfg, polygon)
    try:
        warper = _make_warper(geom, cfg, src_w, src_h, out_w, out_h)
    except Exception:
        warper = None
    yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, geom.src_hfov_deg)
    yaw_min = max(yaw_min - cfg.render_yaw_padding_deg, -geom.src_hfov_deg / 2)
    yaw_max = min(yaw_max + cfg.render_yaw_padding_deg, geom.src_hfov_deg / 2)
    vrot = resolve_video_rotation(
        str(gd / (gj.get("combined_video") or "combined.mp4")),
        gj.get("video_rotation"),
    )

    want = set(range(args.start_g, args.end_g))
    n_done = 0
    last_good = None
    t0 = time.time()
    with av.open(args.out, mode="w") as out_c:
        stream = out_c.add_stream(args.codec, rate=int(round(fps)))
        stream.width = out_w
        stream.height = out_h
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, int(round(fps)))
        stream.codec_context.bit_rate = _parse_bitrate(args.bitrate)
        stream.codec_context.options = {"preset": "veryfast"}
        for g, img in iter_frames_from_segments(
            gd, gj["segments"], want, vrot, hwaccel=not args.no_hwaccel
        ):
            i = g - g0
            if not (0 <= i < len(cmds)):
                continue
            params, view_yaw = _frame_view(
                tuple(cmds[i]),
                geom,
                cfg,
                yaw_min,
                yaw_max,
                src_w,
                src_h,
                out_w,
                out_h,
            )
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            try:
                rendered = _warp_frame(rgb, geom, cfg, params, view_yaw, warper)
                last_good = rendered
            except cv2.error:
                if last_good is None:
                    continue
                rendered = last_good
            frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
            frame.pts = n_done
            for pkt in stream.encode(frame):
                out_c.mux(pkt)
            n_done += 1
            if n_done % 500 == 0:
                el = time.time() - t0
                print(
                    f"{n_done} frames in {el:.0f}s = {n_done / el:.1f} fps", flush=True
                )
        for pkt in stream.encode():
            out_c.mux(pkt)
    if warper is not None:
        warper.close()
    el = time.time() - t0
    print(
        f"rendered {n_done} frames in {el:.0f}s = {n_done / max(el, 1):.1f} fps "
        f"-> {args.out}"
    )


if __name__ == "__main__":
    main()
