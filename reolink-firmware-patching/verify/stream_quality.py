#!/usr/bin/env python3
"""Where the encoder's bits went, and what the picture paid for it.

The complaint this exists to measure is blockiness in the far field. Two things
decide it, and neither is visible in a bitrate number:

* **How the bits are split between keyframes and everything else.** The main
  stream is rate-capped, so every bit a keyframe takes is a bit the nineteen
  frames after it do not get. `stats` reports that split from packet sizes
  alone -- no decode, and exact.
* **What that does to a region of the picture.** `measure` decodes frames and,
  for each named rectangle, reports a blockiness index and a detail figure, both
  overall and by position inside the keyframe cycle.

The blockiness index is reference-free: mean absolute step across pixel
boundaries that lie on the N-pixel coding grid, divided by the mean step across
every other boundary. A picture with no coding structure scores 1.0 on every
grid. Blocks that were quantised hard leave edges on the grid and push it above
1. The grid is anchored to the frame origin, not the rectangle, because that is
where the encoder's blocks are.

Detail is the mean absolute 4-neighbour Laplacian, the same proxy
`roi_qp_heatmap.py` uses: a block coded at a lower QP keeps more of it.

Comparing two clips only means something if the scene and the exposure were the
same and the encoder was RATE-LIMITED in both. A static indoor scene encodes at
about 1 Mbps against a 20 Mbps cap; there the encoder has bits to spare, sits at
its QP floor, and no setting that moves bits around can show an effect. `stats`
prints the measured bitrate so that case is visible instead of being read as
"the change did nothing".

Usage:
    python verify/stream_quality.py stats clip.mp4 [clip2.mp4 ...]
    python verify/stream_quality.py measure clip.mp4 \\
        --region far=2496,180,2816,256 --region near=2496,1100,2560,256
    python verify/stream_quality.py compare before.mp4 after.mp4 --region far=...
    python verify/stream_quality.py capture out.mp4 --seconds 60
    python verify/stream_quality.py selftest
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

import av
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Coding-grid sizes reported. HEVC deblocks on an 8-pixel grid; 64 is the CTU.
GRIDS = (8, 16, 32, 64)

Region = tuple[str, int, int, int, int]  # name, x, y, w, h


# --- packet-level --------------------------------------------------------------


@dataclass
class PacketStats:
    frames: int
    seconds: float
    key_sizes: list[int]
    other_sizes: list[int]
    key_gaps: list[int]

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds else 0.0

    @property
    def mbps(self) -> float:
        total = sum(self.key_sizes) + sum(self.other_sizes)
        return total * 8 / self.seconds / 1e6 if self.seconds else 0.0

    @property
    def key_share(self) -> float:
        total = sum(self.key_sizes) + sum(self.other_sizes)
        return sum(self.key_sizes) / total if total else 0.0

    @property
    def other_mbps(self) -> float:
        return sum(self.other_sizes) * 8 / self.seconds / 1e6 if self.seconds else 0.0


def packet_stats(path: str) -> PacketStats:
    """Keyframe / non-keyframe byte split, from the container. No decode."""
    key: list[int] = []
    other: list[int] = []
    key_at: list[int] = []
    first = last = None
    n = 0
    with av.open(path) as container:
        stream = container.streams.video[0]
        for pkt in container.demux(stream):
            if pkt.size == 0:
                continue
            if pkt.pts is not None:
                t = float(pkt.pts * pkt.time_base)
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
            if pkt.is_keyframe:
                key.append(pkt.size)
                key_at.append(n)
            else:
                other.append(pkt.size)
            n += 1
    if n < 2 or first is None or last is None or last <= first:
        raise SystemExit(f"{path}: not enough timestamped video packets to measure")
    # N frames span N-1 intervals; scale to the time N frames actually cover.
    seconds = (last - first) * n / (n - 1)
    gaps = sorted({b - a for a, b in zip(key_at, key_at[1:], strict=False)})
    return PacketStats(n, seconds, key, other, gaps)


def print_stats(path: str, s: PacketStats) -> None:
    print(f"{os.path.basename(path)}")
    print(f"  {s.frames} frames over {s.seconds:.1f} s = {s.fps:.2f} fps")
    print(f"  video bitrate        {s.mbps:6.2f} Mbps")
    if not s.key_sizes or not s.other_sizes:
        print("  (clip does not contain both keyframes and non-keyframes)")
        return
    print(
        f"  keyframes            {len(s.key_sizes)}, every {s.key_gaps or '?'} frames, "
        f"mean {np.mean(s.key_sizes) / 1e3:.0f} KB"
    )
    print(f"  other frames         mean {np.mean(s.other_sizes) / 1e3:.1f} KB")
    print(
        f"  keyframe share       {100 * s.key_share:5.1f} %  "
        f"({s.mbps - s.other_mbps:.2f} Mbps keyframes, {s.other_mbps:.2f} Mbps the rest)"
    )


# --- picture-level -------------------------------------------------------------


def grid_index(gray: np.ndarray, x0: int, y0: int, n: int) -> float:
    """Blockiness on the n-pixel grid for a crop whose top-left is (x0, y0).

    Mean |step| across boundaries on the grid over mean |step| across the rest,
    averaged over the horizontal and vertical directions.
    """
    out = []
    for axis, origin in ((1, x0), (0, y0)):
        step = np.abs(np.diff(gray, axis=axis))
        # step[i] is the boundary between pixel i and i+1 of the crop, i.e. it
        # sits at frame coordinate origin + i + 1.
        pos = origin + np.arange(step.shape[axis]) + 1
        on = pos % n == 0
        if not on.any() or on.all():
            continue
        on_mean = np.compress(on, step, axis=axis).mean()
        off_mean = np.compress(~on, step, axis=axis).mean()
        if off_mean > 0:
            out.append(on_mean / off_mean)
    return float(np.mean(out)) if out else float("nan")


def detail(gray: np.ndarray) -> float:
    """Mean |4-neighbour Laplacian| of a crop."""
    c = gray[1:-1, 1:-1]
    lap = 4 * c - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return float(np.abs(lap).mean())


def region_row(gray: np.ndarray, region: Region) -> list[float]:
    _, x, y, w, h = region
    crop = gray[y : y + h, x : x + w].astype(np.float32)
    return [*(grid_index(crop, x, y, n) for n in GRIDS), detail(crop)]


def parse_region(text: str) -> Region:
    try:
        name, rect = text.split("=", 1)
        x, y, w, h = (int(v) for v in rect.split(","))
    except ValueError:
        raise SystemExit(
            f"--region wants name=x,y,w,h in pixels, got {text!r}"
        ) from None
    if w < 2 * GRIDS[-1] or h < 2 * GRIDS[-1]:
        raise SystemExit(
            f"region {name!r} is smaller than two {GRIDS[-1]}-pixel blocks"
        )
    return (name, x, y, w, h)


def default_regions(width: int, height: int) -> list[Region]:
    """Four full-width horizontal bands, when no --region is given."""
    band = height // 4
    return [
        (f"rows {i * band}-{(i + 1) * band - 1}", 0, i * band, width, band)
        for i in range(4)
    ]


def measure(
    path: str, regions: list[Region] | None, frames: int, start: float
) -> tuple[list[Region], dict[str, dict[str, np.ndarray]]]:
    """Per-region metrics averaged over `frames` frames from `start` seconds in.

    Returns, per region, the mean over all frames, over keyframes only, and over
    the last frame of each keyframe cycle (the frame furthest from a keyframe,
    where a starved stream looks worst).
    """
    rows: dict[str, list[list[float]]] = {}
    is_key: list[bool] = []
    with av.open(path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        ctx = stream.codec_context
        if regions is None:
            regions = default_regions(ctx.width, ctx.height)
        for name, x, y, w, h in regions:
            if x < 0 or y < 0 or x + w > ctx.width or y + h > ctx.height:
                raise SystemExit(
                    f"region {name!r} ({x},{y} {w}x{h}) is outside the "
                    f"{ctx.width}x{ctx.height} frame"
                )
            rows[name] = []
        if start > 0:
            container.seek(int(start / stream.time_base), stream=stream)
        for frame in container.decode(stream):
            gray = frame.to_ndarray(format="gray")
            for region in regions:
                rows[region[0]].append(region_row(gray, region))
            is_key.append(bool(frame.key_frame))
            if len(is_key) >= frames:
                break
    if not is_key:
        raise SystemExit(f"{path}: no frames decoded")
    key = np.array(is_key)
    # Last frame of a cycle = the frame immediately before a keyframe.
    tail = np.zeros(len(key), dtype=bool)
    tail[:-1] = key[1:]
    out = {}
    for name, data in rows.items():
        arr = np.array(data)
        out[name] = {
            "all": arr.mean(axis=0),
            "key": arr[key].mean(axis=0)
            if key.any()
            else np.full(arr.shape[1], np.nan),
            "tail": arr[tail].mean(axis=0)
            if tail.any()
            else np.full(arr.shape[1], np.nan),
        }
    return regions, out


_HEAD = "blk8   blk16  blk32  blk64  detail"


def _fmt(v: np.ndarray) -> str:
    return "  ".join(f"{x:5.3f}" for x in v[:-1]) + f"  {v[-1]:6.3f}"


def print_measure(
    path: str, regions: list[Region], m: dict[str, dict[str, np.ndarray]]
) -> None:
    print(f"{os.path.basename(path)}")
    for name, x, y, w, h in regions:
        print(f"  region {name}  ({x},{y} {w}x{h})")
        print(f"    {'':22s}{_HEAD}")
        for label, key in (
            ("all frames", "all"),
            ("keyframes", "key"),
            ("last frame of cycle", "tail"),
        ):
            print(f"    {label:22s}{_fmt(m[name][key])}")


# --- capture -------------------------------------------------------------------


def load_env(path: str) -> dict[str, str]:
    """Parse the shell-style camera.env that _camera_env.sh also sources."""
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        raise SystemExit(
            f"{path} not found; copy camera.env.example and fill it in"
        ) from None
    for required in ("CAMERA_IP", "CAMERA_USER", "CAMERA_PASS"):
        if not env.get(required):
            raise SystemExit(f"{required} missing from {path}")
    return env


def capture(source: str, out: str, seconds: float, options: dict[str, str]) -> int:
    """Copy the video stream of `source` into `out` for `seconds`. No re-encode.

    The camera will not record on the home network (S99_NetState sees to that,
    and it should), so at home the main stream is taken live instead. It is the
    same encoder output the recorder writes: there is one 7680x2160 encode path.
    Packets are copied untouched, so sizes and keyframe flags are the encoder's.
    """
    written = 0
    with av.open(source, options=options, timeout=20) as src:
        in_stream = src.streams.video[0]
        with av.open(out, "w") as dst:
            out_stream = dst.add_stream_from_template(in_stream)
            t0 = None
            base = None
            for pkt in src.demux(in_stream):
                if pkt.size == 0 or pkt.dts is None or pkt.pts is None:
                    continue
                if base is None:
                    if not pkt.is_keyframe:
                        continue  # a clip that starts mid-cycle cannot be decoded
                    base = pkt.dts
                    t0 = time.monotonic()
                if float((pkt.dts - base) * pkt.time_base) >= seconds:
                    break
                pkt.pts -= base
                pkt.dts -= base
                pkt.stream = out_stream
                dst.mux(pkt)
                written += 1
            if t0 is None:
                raise SystemExit(f"{source}: no keyframe arrived; nothing captured")
    return written


# --- self-test -----------------------------------------------------------------


def _selftest() -> int:
    rng = np.random.default_rng(0)
    fails = 0

    def expect(name: str, ok: bool, got: str) -> None:
        nonlocal fails
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {got}")
        fails += 0 if ok else 1

    noise = rng.normal(128, 20, (512, 768)).astype(np.float32)
    for n in GRIDS:
        v = grid_index(noise, 0, 0, n)
        expect(
            f"structureless picture scores ~1 on the {n} grid",
            abs(v - 1) < 0.05,
            f"{v:.3f}",
        )

    # Each 16x16 block flattened to its mean, plus a little noise: edges exist
    # only on the 16 grid (and so on 32 and 64, which are subsets of it).
    blocky = noise.reshape(32, 16, 48, 16).mean(axis=(1, 3))
    blocky = np.repeat(np.repeat(blocky, 16, axis=0), 16, axis=1)
    blocky += rng.normal(0, 0.2, blocky.shape).astype(np.float32)
    v16 = grid_index(blocky, 0, 0, 16)
    expect("16-pixel blocks score far above 1 on the 16 grid", v16 > 5, f"{v16:.2f}")

    # The grid is anchored to the frame, not the crop: the same picture cropped
    # 5 pixels in must still find its edges, given the crop's true origin...
    shifted = grid_index(blocky[5:, 5:], 5, 5, 16)
    expect("a crop is scored against the frame's grid", shifted > 5, f"{shifted:.2f}")
    # ...and must lose them if the origin is ignored.
    wrong = grid_index(blocky[5:, 5:], 0, 0, 16)
    expect("ignoring the crop origin loses the edges", wrong < 1.05, f"{wrong:.2f}")

    expect(
        "flattening blocks removes detail",
        detail(blocky) < 0.2 * detail(noise),
        f"{detail(blocky):.2f} vs {detail(noise):.2f}",
    )
    print("selftest:", "FAILED" if fails else "passed")
    return 1 if fails else 0


# --- cli -----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="keyframe share and bitrate, no decode")
    p.add_argument("clips", nargs="+")

    def add_measure_args(q: argparse.ArgumentParser) -> None:
        q.add_argument(
            "--region",
            action="append",
            default=None,
            metavar="NAME=X,Y,W,H",
            help="rectangle to score, in frame pixels; repeatable",
        )
        q.add_argument("--frames", type=int, default=120, help="frames to decode")
        q.add_argument("--start", type=float, default=0.0, help="seconds to skip first")

    p = sub.add_parser("measure", help="per-region blockiness and detail")
    p.add_argument("clip")
    add_measure_args(p)

    p = sub.add_parser("compare", help="stats and per-region metrics, two clips")
    p.add_argument("before")
    p.add_argument("after")
    add_measure_args(p)

    p = sub.add_parser("capture", help="copy the live main stream to a file")
    p.add_argument("out")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--env", default=os.path.join(ROOT, "camera.env"))
    p.add_argument(
        "--url",
        default=None,
        help="source instead of the camera's main stream (any URL or file ffmpeg reads)",
    )

    sub.add_parser("selftest", help="check the metrics against synthetic pictures")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return _selftest()

    if args.cmd == "stats":
        for clip in args.clips:
            print_stats(clip, packet_stats(clip))
        return 0

    if args.cmd == "capture":
        if args.url:
            source, shown, options = args.url, args.url, {}
        else:
            env = load_env(args.env)
            path = f"{env['CAMERA_IP']}:554/h264Preview_01_main"
            source = f"rtsp://{env['CAMERA_USER']}:{env['CAMERA_PASS']}@{path}"
            shown = f"rtsp://{path}"
            options = {"rtsp_transport": "tcp"}
        n = capture(source, args.out, args.seconds, options)
        print(f"captured {n} packets from {shown} -> {args.out}")
        print_stats(args.out, packet_stats(args.out))
        return 0

    regions = [parse_region(r) for r in args.region] if args.region else None

    if args.cmd == "measure":
        print_stats(args.clip, packet_stats(args.clip))
        used, m = measure(args.clip, regions, args.frames, args.start)
        print_measure(args.clip, used, m)
        return 0

    # compare
    sa, sb = packet_stats(args.before), packet_stats(args.after)
    print_stats(args.before, sa)
    print_stats(args.after, sb)
    used, ma = measure(args.before, regions, args.frames, args.start)
    _, mb = measure(args.after, used, args.frames, args.start)
    print(
        "\nafter / before  (blockiness: below 1 is better; detail: above 1 is better)"
    )
    for name, *_ in used:
        print(f"  region {name}")
        print(f"    {'':22s}{_HEAD}")
        for label, key in (
            ("all frames", "all"),
            ("keyframes", "key"),
            ("last frame of cycle", "tail"),
        ):
            # Blockiness is an excess over 1.0, so compare the excess.
            a, b = ma[name][key], mb[name][key]
            ratio = np.append(
                (b[:-1] - 1) / np.maximum(a[:-1] - 1, 1e-6), b[-1] / a[-1]
            )
            print(f"    {label:22s}{_fmt(ratio)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
