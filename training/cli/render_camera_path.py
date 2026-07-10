"""Render a camera_path/1 artifact through the PRODUCTION render step code.

Thin evaluation wrapper around :mod:`video_grouper.pipeline.steps.render` — the
same module the shipped pipeline runs (dumb-renderer camera-path mode, Mark
2026-07-10: what we review must be what we ship). Two access modes share every
line of render math (``_frame_view`` + ``_warp_frame``):

- **full video** (no window): delegates to the production ``_render_video`` with
  ``camera_path_file`` — byte-identical to the pipeline's render step output.
- **clip window** (``--start-g/--end-g``): decodes exact global-frame ranges from
  the RAW segments (frame-exact, corruption-isolated) and executes the same
  per-frame solve/warp — for fast iteration on adjudicated windows. This mode
  also composites a REVIEW MINIMAP in the lower-right (full-frame thumbnail with
  our viewport rectangle + AutoCam's focus marker) so ball losses / camera
  disagreements are obvious at a glance. The minimap is an eval-only overlay
  (needs AutoCam's viewport, a comparison artifact) — the shipped production
  render never draws it.

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


def _draw_minimap(
    rendered,
    src_bgr,
    our_cx: float,
    our_cy: float,
    our_hfov: float,
    ac_xy,
    ball_xy,
    ball_trail,
    gt_xy,
    polygon,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
):
    """Composite a review minimap into the lower-right of ``rendered`` (RGB, mutated
    in place): dimmed full-frame thumbnail with the field outline (yellow), OUR
    viewport rectangle (magenta), AutoCam's focus (green cross), the raw ball
    DETECTION (cyan dot + trail), and the human GT ball (green arrow) when labeled."""
    import cv2
    import numpy as np

    mw = out_w // 4
    mh = max(1, int(round(mw * src_h / src_w)))
    mini = cv2.cvtColor(cv2.resize(src_bgr, (mw, mh)), cv2.COLOR_BGR2RGB)
    mini = (mini.astype(np.float32) * 0.6).astype(np.uint8)  # dim so overlays pop
    sx, sy = mw / src_w, mh / src_h

    if polygon is not None:
        pp = (np.asarray(polygon, float) * np.array([sx, sy])).astype(np.int32)
        cv2.polylines(mini, [pp], True, (255, 255, 0), 1)

    # ball trail (past ~1 s) — fade older segments toward dim, current is brightest
    if ball_trail:
        pts = [(int(x * sx), int(y * sy)) for (x, y) in ball_trail if x is not None]
        for k in range(1, len(pts)):
            f = k / max(len(pts) - 1, 1)  # 0 old -> 1 recent
            col = (0, int(120 + 135 * f), int(120 + 135 * f))  # dim->bright cyan
            cv2.line(mini, pts[k - 1], pts[k], col, 1)
    if ball_xy is not None:
        bx, by = int(ball_xy[0] * sx), int(ball_xy[1] * sy)
        cv2.circle(mini, (bx, by), 4, (0, 255, 255), -1)
        cv2.circle(mini, (bx, by), 4, (0, 0, 0), 1)

    hw = src_w * (our_hfov / 180.0) / 2.0 * sx
    hh = hw * (out_h / out_w)
    ox, oy = int(our_cx * sx), int(our_cy * sy)
    cv2.rectangle(
        mini,
        (int(ox - hw), int(oy - hh)),
        (int(ox + hw), int(oy + hh)),
        (255, 0, 255),
        2,
    )
    cv2.circle(mini, (ox, oy), 3, (255, 0, 255), -1)
    if ac_xy is not None:
        cv2.drawMarker(
            mini,
            (int(ac_xy[0] * sx), int(ac_xy[1] * sy)),
            (0, 255, 0),
            cv2.MARKER_TILTED_CROSS,
            14,
            2,
        )
    if gt_xy is not None:
        gx, gy = int(gt_xy[0] * sx), int(gt_xy[1] * sy)
        cv2.arrowedLine(
            mini, (gx - 13, gy - 13), (gx, gy), (0, 255, 0), 2, tipLength=0.4
        )
    cv2.putText(mini, "OURS", (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)
    cv2.putText(mini, "AC", (66, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
    cv2.putText(
        mini, "BALL", (108, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1
    )
    cv2.putText(mini, "GT", (170, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 220, 0), 1)
    cv2.rectangle(mini, (0, 0), (mw - 1, mh - 1), (255, 255, 255), 2)
    y0, x0 = out_h - mh - 12, out_w - mw - 12
    rendered[y0 : y0 + mh, x0 : x0 + mw] = mini


def _project_src_to_out(map_x, map_y, pt, sub: int = 4):
    """Project a SOURCE pixel ``pt`` to the output pixel that samples nearest it
    (via the warp maps, ``sub``-subsampled for speed). None if ``pt`` is outside
    the current view (no map entry within tolerance)."""
    import numpy as np

    if pt is None or pt[0] is None:
        return None
    mxs, mys = map_x[::sub, ::sub], map_y[::sub, ::sub]
    d2 = (mxs - float(pt[0])) ** 2 + (mys - float(pt[1])) ** 2
    j = int(np.argmin(d2))
    v, u = np.unravel_index(j, d2.shape)
    if d2[v, u] <= (6.0 * sub) ** 2:
        return (int(u * sub), int(v * sub))
    return None


def _draw_ball_on_frame(rendered, map_x, map_y, ball_xy, trail):
    """Draw the raw SELECTED DETECTION (cyan ring) + recent-detections trail on the
    RENDERED view. A detection outside the current view is skipped."""
    import cv2

    if trail:
        proj_pts = [_project_src_to_out(map_x, map_y, p) for p in trail]
        prev = None
        for k, pp in enumerate(proj_pts):
            if prev is not None and pp is not None:
                f = k / max(len(proj_pts) - 1, 1)
                cv2.line(
                    rendered, prev, pp, (0, int(140 + 115 * f), int(140 + 115 * f)), 2
                )
            prev = pp
    bp = _project_src_to_out(map_x, map_y, ball_xy)
    if bp is not None:
        cv2.circle(rendered, bp, 14, (0, 0, 0), 3)
        cv2.circle(rendered, bp, 14, (0, 255, 255), 2)
        cv2.circle(rendered, bp, 3, (0, 255, 255), -1)


def _draw_field_outline(rendered, map_x, map_y, polygon):
    """Draw the field outline (yellow) on the rendered view. Each polygon edge is
    interpolated + projected so the touchlines curve correctly in the warp."""
    import cv2
    import numpy as np

    poly = np.asarray(polygon, float)
    n = len(poly)
    proj = []
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        for t in np.linspace(0.0, 1.0, 16, endpoint=False):
            p = a + t * (b - a)
            proj.append(_project_src_to_out(map_x, map_y, (p[0], p[1])))
    prev = proj[-1]
    for pp in proj:
        if prev is not None and pp is not None:
            cv2.line(rendered, prev, pp, (255, 255, 0), 2)
        prev = pp


def _draw_gt_arrow(rendered, map_x, map_y, gt_xy):
    """Draw a green arrow pointing at the HUMAN GT ball position (truth) on the
    rendered view, when the labeled point is within the current frame."""
    import cv2

    gp = _project_src_to_out(map_x, map_y, gt_xy)
    if gp is None:
        return
    tip = (int(gp[0]), int(gp[1]))
    tail = (int(gp[0]) - 46, int(gp[1]) - 46)  # arrow from upper-left into the point
    cv2.arrowedLine(rendered, tail, tip, (0, 0, 0), 5, tipLength=0.35)
    cv2.arrowedLine(rendered, tail, tip, (0, 255, 0), 3, tipLength=0.35)
    cv2.putText(
        rendered,
        "GT",
        (tail[0] - 30, tail[1]),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera-path", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start-g", type=int, default=None)
    ap.add_argument("--end-g", type=int, default=None)
    ap.add_argument("--bitrate", default="8M")
    ap.add_argument("--codec", default="h264")
    ap.add_argument(
        "--no-minimap",
        action="store_true",
        help="disable the lower-right review minimap (clip-window mode)",
    )
    ap.add_argument(
        "--no-ball-overlay",
        action="store_true",
        help="disable the tracked-ball dot + trail drawn on the full frame",
    )
    ap.add_argument(
        "--no-gt-overlay",
        action="store_true",
        help="disable the human-GT green arrow (drawn on labeled frames)",
    )
    ap.add_argument(
        "--no-field-outline",
        action="store_true",
        help="disable the yellow field-outline overlay",
    )
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    import av
    import cv2
    import numpy as np

    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation
    from video_grouper.inference.cylindrical_view import yaw_pitch_to_pixel
    from video_grouper.inference.field_geometry import field_lateral_yaw_extent
    from video_grouper.pipeline.steps.render import (
        RenderStepConfig,
        _frame_view,
        _parse_bitrate,
        _project_maps,
        _render_video,
        _resolve_geometry,
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
    # clip-window debug renderer uses the cv2 map path (_project_maps + remap) so the
    # ball can be projected source->output; the opencl warper is skipped here.
    yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, geom.src_hfov_deg)
    yaw_min = max(yaw_min - cfg.render_yaw_padding_deg, -geom.src_hfov_deg / 2)
    yaw_max = min(yaw_max + cfg.render_yaw_padding_deg, geom.src_hfov_deg / 2)
    vrot = resolve_video_rotation(
        str(gd / (gj.get("combined_video") or "combined.mp4")),
        gj.get("video_rotation"),
    )

    # AutoCam viewport for the minimap marker (eval-only comparison artifact).
    ac_vp: dict[int, tuple[float, float]] = {}
    if not args.no_minimap:
        from training.data_prep import distill_dataset as dd

        offs = dd.seg_offsets(gj["segments"])
        vpp = gd / "autocam_viewport.jsonl"
        if vpp.exists():
            for ln in vpp.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.strip():
                    r = json.loads(ln)
                    sg = offs.get(r.get("seg"))
                    if sg is not None:
                        ac_vp[sg + int(r["f"])] = (float(r["x"]), float(r["y"]))

    # ball sidecar (from plan_camera_path): draw the RAW per-frame SELECTED DETECTION
    # (where the ball was actually found), NOT the smoothed track — the smoothed track
    # lags/leads and interpolates between the stride-spaced detections.
    det_frames: np.ndarray = np.zeros(0, dtype=int)
    det_pos: dict[int, list] = {}
    tp = Path(args.camera_path).with_suffix(".track.json")
    if tp.exists():
        td = json.loads(tp.read_text(encoding="utf-8"))
        det_pos = {int(k): v for k, v in td.get("detections", {}).items()}
        det_frames = np.array(sorted(det_pos), dtype=int)
    trail_len = int(round(2 * fps))  # ~2 s (detections are stride-spaced)

    def _nearest_det(g: int, tol: int = 4):
        """The selected detection at the nearest sampled frame within +-tol."""
        if len(det_frames) == 0:
            return None
        j = int(np.searchsorted(det_frames, g))
        best, bd = None, tol + 1
        for k in (j - 1, j):
            if 0 <= k < len(det_frames):
                d = abs(int(det_frames[k]) - g)
                if d < bd:
                    best, bd = int(det_frames[k]), d
        return det_pos[best] if best is not None else None

    # human GT ball labels (truth): exact per-frame -> green arrow when labeled
    gt: dict[int, tuple[float, float]] = {}
    if not args.no_gt_overlay:
        from training.data_prep import distill_dataset as dd

        hb, _ = (
            dd.load_human_labels(
                gd / "ball_labels.jsonl", dd.seg_offsets(gj["segments"])
            )
            if (gd / "ball_labels.jsonl").exists()
            else ({}, set())
        )
        gt = {int(k): (float(v[0]), float(v[1])) for k, v in hb.items()}

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
            # compute the warp maps explicitly (same cv2 remap the production render
            # uses) so the ball can be projected source->output onto the full frame.
            try:
                map_x, map_y = _project_maps(geom, cfg, params, view_yaw)
                rendered = cv2.remap(
                    rgb, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT
                )
                last_good = (rendered, map_x, map_y)
            except cv2.error:
                if last_good is None:
                    continue
                rendered, map_x, map_y = last_good

            # RAW selected detection at (or nearest to) this frame + recent detections
            ball_xy = _nearest_det(g)
            trail = None
            if len(det_frames):
                lo = np.searchsorted(det_frames, g - trail_len)
                hi = np.searchsorted(det_frames, g + 1)
                trail = [det_pos[int(det_frames[k])] for k in range(lo, hi)]

            gt_xy = gt.get(g)
            rendered = (
                rendered.copy()
            )  # overlays draw here; keep last_good a clean warp
            if not args.no_field_outline:
                _draw_field_outline(rendered, map_x, map_y, polygon)
            if not args.no_ball_overlay and ball_xy is not None:
                _draw_ball_on_frame(rendered, map_x, map_y, ball_xy, trail)
            if gt_xy is not None:
                _draw_gt_arrow(rendered, map_x, map_y, gt_xy)
            if not args.no_minimap:
                ocx, ocy = yaw_pitch_to_pixel(
                    view_yaw,
                    params.view_pitch_deg + params.view_pitch_offset_deg,
                    src_w,
                    src_h,
                    params.src_hfov_deg,
                )
                _draw_minimap(
                    rendered,
                    img,
                    ocx,
                    ocy,
                    float(cmds[i][2]),
                    ac_vp.get(g),
                    ball_xy,
                    trail,
                    gt_xy,
                    polygon,
                    src_w,
                    src_h,
                    out_w,
                    out_h,
                )
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
    el = time.time() - t0
    print(
        f"rendered {n_done} frames in {el:.0f}s = {n_done / max(el, 1):.1f} fps "
        f"-> {args.out}"
    )


if __name__ == "__main__":
    main()
