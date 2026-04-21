"""Benchmark per-row dx remap candidates using PyAV.

Decodes a fixed-duration clip from a large raw file, applies a piecewise-linear
dx(y) profile to the right half only (x >= SEAM_X), re-encodes with hevc_qsv
when available (or libx265 fallback), and reports wall-clock / realtime ratio.

All candidates produce visually-equivalent output. Written to match the
existing PyAV pattern in video_grouper/utils/ffmpeg_utils.py.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import av
import numpy as np

SEAM_X = 3840
FRAME_W = 7680
FRAME_H = 2160

# Anchors measured in the throwaway experiment (Mark's install).
DX_ANCHORS = [(0, -10), (477, -20), (657, -35), (1500, 0), (2160, 0)]


def build_dx_profile(anchors: list[tuple[int, int]], h: int = FRAME_H) -> np.ndarray:
    ys = np.array([a[0] for a in anchors], dtype=np.float32)
    dxs = np.array([a[1] for a in anchors], dtype=np.float32)
    y_idx = np.arange(h, dtype=np.float32)
    return np.round(np.interp(y_idx, ys, dxs)).astype(np.int32)  # integer per-row shift


def _open_output(output_path: Path, template_stream, encoder: str):
    """Open an output container with an HEVC encoder matching the template.

    Falls back libx265 if hevc_qsv is unavailable or errors out.
    """
    container = av.open(str(output_path), mode="w", options={"movflags": "faststart"})
    try:
        stream = container.add_stream(encoder, rate=template_stream.average_rate or 20)
    except Exception:
        container.close()
        raise
    stream.width = template_stream.codec_context.width
    stream.height = template_stream.codec_context.height
    stream.pix_fmt = "nv12" if encoder.endswith("_qsv") else "yuv420p"
    # QSV-specific quality; libx265 uses CRF through options.
    if encoder.endswith("_qsv"):
        stream.options = {"preset": "veryfast", "global_quality": "24"}
    else:
        stream.options = {"preset": "medium", "crf": "24"}
    return container, stream


def _shift_frame_rgb(arr: np.ndarray, dx_profile: np.ndarray) -> np.ndarray:
    """Candidate A: full-frame RGB shift via per-row roll. Returns same shape."""
    out = arr.copy()
    right = out[:, SEAM_X:]
    for y in range(arr.shape[0]):
        dx = int(dx_profile[y])
        if dx == 0:
            continue
        # Roll the right half row leftward by |dx| (so the pixel that was at col k
        # ends up at col k+dx); clip to valid range.
        right[y] = np.roll(right[y], dx, axis=0)
    out[:, SEAM_X:] = right
    return out


def _shift_frame_nv12(
    y_plane: np.ndarray, uv_plane: np.ndarray, dx_profile: np.ndarray
) -> None:
    """Candidate B: in-place NV12 per-row shift; skips rows where dx==0.

    y_plane shape (H, W). uv_plane shape (H/2, W) — interleaved U/V per pixel pair.
    For the right half (x >= SEAM_X on Y plane; x >= SEAM_X on UV), roll rows by dx.
    """
    H_y = y_plane.shape[0]
    H_uv = uv_plane.shape[0]
    # Precompute which rows need work (dx != 0). Rows with dx==0 are skipped entirely.
    nonzero_y = np.nonzero(dx_profile[:H_y])[0]
    for y in nonzero_y:
        dx = int(dx_profile[y])
        y_plane[y, SEAM_X:] = np.roll(y_plane[y, SEAM_X:], dx)

    # UV plane: every 2 luma rows share one chroma row.
    for y_uv in range(H_uv):
        y_luma = y_uv * 2
        dx_luma = int(dx_profile[min(y_luma, dx_profile.size - 1)])
        dx_uv = (dx_luma // 2) * 2
        if dx_uv == 0:
            continue
        uv_plane[y_uv, SEAM_X:] = np.roll(uv_plane[y_uv, SEAM_X:], dx_uv)


def _shift_frame_nv12_halfonly(
    y_plane: np.ndarray, uv_plane: np.ndarray, dx_profile: np.ndarray
) -> None:
    """Candidate C: same as B but only touch the right half slice (memory-bandwidth saver).

    np.roll on a strided view already only reads/writes the right half rows; B and C
    are effectively the same in numpy. Kept separate for explicit clarity and in case
    we want a future C++ inner loop that exploits this.
    """
    _shift_frame_nv12(y_plane, uv_plane, dx_profile)


def run_candidate(
    label: str,
    input_path: Path,
    output_path: Path,
    duration_s: float,
    dx_profile: np.ndarray,
    mode: str,
    encoder: str,
) -> dict:
    """Run one candidate end-to-end. Returns timing stats plus per-phase breakdown."""
    phase_decode = 0.0
    phase_shift = 0.0
    phase_encode = 0.0
    t0 = time.perf_counter()

    # Try QSV hwaccel decode; fall back to software if unavailable
    try:
        in_container = av.open(str(input_path), options={"hwaccel": "qsv"})
    except Exception:
        in_container = av.open(str(input_path))
    in_video = next(s for s in in_container.streams if s.type == "video")
    time_base = in_video.time_base
    stream_h = in_video.codec_context.height

    out_container, out_video = _open_output(output_path, in_video, encoder)

    start_pts = None
    end_pts_limit = int(duration_s / time_base) if time_base else None
    frames_processed = 0

    try:
        for packet in in_container.demux(in_video):
            if packet.dts is None:
                continue
            if start_pts is None and packet.pts is not None:
                start_pts = packet.pts
            if (
                end_pts_limit is not None
                and packet.pts is not None
                and start_pts is not None
                and (packet.pts - start_pts) > end_pts_limit
            ):
                break
            td0 = time.perf_counter()
            decoded_frames = list(packet.decode())
            phase_decode += time.perf_counter() - td0
            for frame in decoded_frames:
                ts0 = time.perf_counter()
                if mode == "passthrough":
                    new_frame = frame
                elif mode == "rgb":
                    arr = frame.to_ndarray(format="rgb24")
                    shifted = _shift_frame_rgb(arr, dx_profile)
                    new_frame = av.VideoFrame.from_ndarray(shifted, format="rgb24")
                elif mode == "nv12":
                    nv12_arr = frame.to_ndarray(format="nv12")
                    y_plane = nv12_arr[:stream_h]
                    uv_plane = nv12_arr[stream_h:]
                    _shift_frame_nv12(y_plane, uv_plane, dx_profile)
                    new_frame = av.VideoFrame.from_ndarray(nv12_arr, format="nv12")
                elif mode == "nv12_half":
                    nv12_arr = frame.to_ndarray(format="nv12")
                    y_plane = nv12_arr[:stream_h]
                    uv_plane = nv12_arr[stream_h:]
                    _shift_frame_nv12_halfonly(y_plane, uv_plane, dx_profile)
                    new_frame = av.VideoFrame.from_ndarray(nv12_arr, format="nv12")
                elif mode == "nv12_vec":
                    nv12_arr = frame.to_ndarray(format="nv12")
                    y_plane = nv12_arr[:stream_h]
                    uv_plane = nv12_arr[stream_h:]
                    _shift_frame_nv12_vectorized(y_plane, uv_plane, dx_profile)
                    new_frame = av.VideoFrame.from_ndarray(nv12_arr, format="nv12")
                else:
                    raise ValueError(f"unknown mode {mode}")
                new_frame.pts = frame.pts
                new_frame.time_base = frame.time_base
                phase_shift += time.perf_counter() - ts0
                te0 = time.perf_counter()
                for out_packet in out_video.encode(new_frame):
                    out_container.mux(out_packet)
                phase_encode += time.perf_counter() - te0
                frames_processed += 1
        te0 = time.perf_counter()
        for out_packet in out_video.encode(None):
            out_container.mux(out_packet)
        phase_encode += time.perf_counter() - te0
    finally:
        out_container.close()
        in_container.close()

    wall = time.perf_counter() - t0
    video_seconds = frames_processed / float(in_video.average_rate or 20)
    ratio = wall / video_seconds if video_seconds > 0 else float("inf")
    return {
        "label": label,
        "wall_s": round(wall, 2),
        "decode_s": round(phase_decode, 2),
        "shift_s": round(phase_shift, 2),
        "encode_s": round(phase_encode, 2),
        "frames": frames_processed,
        "video_s": round(video_seconds, 2),
        "realtime_x": round(ratio, 2),
        "output_mb": round(output_path.stat().st_size / 1024**2, 1),
    }


def _shift_frame_nv12_vectorized(
    y_plane: np.ndarray, uv_plane: np.ndarray, dx_profile: np.ndarray
) -> None:
    """Fully vectorized per-row shift using fancy indexing. No Python loop over y."""
    H_y, W = y_plane.shape
    # Right half of Y plane: shape (H_y, W_right).
    W_right = W - SEAM_X
    right_y = y_plane[:, SEAM_X:]
    # Build per-row source-column indices: out[y, x] = right_y[y, (x + dx[y]) % W_right]
    cols = np.arange(W_right, dtype=np.int32)[None, :]  # (1, W_right)
    dx_y = dx_profile[:H_y, None]  # (H_y, 1)
    src_cols = (cols - dx_y) % W_right  # dx is pull-from offset, so add
    # Actually we want "shift right half left by |dx|" which is np.roll(right_y, dx).
    # np.roll(a, k) moves element at i to i+k; equivalently a[(i-k) % n] goes to i.
    # So out[y, x] = right_y[y, (x - dx[y]) % W_right]
    src_cols = (cols - dx_y) % W_right
    y_plane[:, SEAM_X:] = np.take_along_axis(right_y, src_cols, axis=1)

    # UV plane: subsampled 2:1 vertically, interleaved horizontally. Must keep U/V pairs.
    H_uv = uv_plane.shape[0]
    dx_uv = dx_profile[0 : H_uv * 2 : 2]  # take even y values; align to pairs
    dx_uv = (dx_uv // 2) * 2  # keep dx even so U and V stay paired
    right_uv = uv_plane[:, SEAM_X:]
    cols_uv = np.arange(W_right, dtype=np.int32)[None, :]
    src_cols_uv = (cols_uv - dx_uv[:, None]) % W_right
    uv_plane[:, SEAM_X:] = np.take_along_axis(right_uv, src_cols_uv, axis=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="raw 7680x2160 H.265 clip")
    ap.add_argument("--out-dir", required=True, help="where to write benchmark outputs")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds to process")
    ap.add_argument(
        "--encoder", default="hevc_qsv", help="hevc_qsv | libx265 (fallback)"
    )
    ap.add_argument(
        "--candidates", default="A,B,C", help="comma-separated subset of A,B,C"
    )
    args = ap.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dx_profile = build_dx_profile(DX_ANCHORS)
    print(
        f"dx profile: y=0 -> {dx_profile[0]}, y=1080 -> {dx_profile[1080]}, "
        f"y=2159 -> {dx_profile[2159]}"
    )

    plans = {
        "A": ("rgb", "A_rgb_fullframe"),
        "B": ("nv12", "B_nv12_inplace"),
        "C": ("nv12_half", "C_nv12_halfonly"),
        "V": ("nv12_vec", "V_nv12_vectorized"),
        "P": ("passthrough", "P_passthrough"),  # decode+encode only, no shift
    }

    results = []
    for letter in args.candidates.split(","):
        letter = letter.strip()
        if letter not in plans:
            continue
        mode, name = plans[letter]
        out_path = out_dir / f"{name}.mp4"
        print(f"\n=== Candidate {letter} ({mode}, encoder={args.encoder}) ===")
        try:
            r = run_candidate(
                letter,
                input_path,
                out_path,
                args.duration,
                dx_profile,
                mode,
                args.encoder,
            )
            results.append(r)
            print(r)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            results.append({"label": letter, "error": str(e)})

    print("\n=== SUMMARY ===")
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
