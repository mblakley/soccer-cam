"""Generate the synthetic recordings the E2E camera simulator serves.

``tests/e2e/test_clips/`` is gitignored, so a fresh clone has no fixture data:
the simulator mounts an empty directory, serves nothing, and
``test_complete_pipeline`` ends at ``groups_created: 0/2`` no matter what the
pipeline does. This regenerates that fixture from nothing, so the E2E suite is
runnable without hunting down real footage.

Synthetic on purpose. The E2E test exercises *plumbing* — discover, download,
group, combine, trim, hand off, upload — none of which cares what is in the
frames. Real game footage would be gigabytes, cannot be committed, and would
make the fixture depend on a specific match.

Run::

    uv run python -m tests.e2e.make_test_clips

Written with PyAV rather than an ffmpeg subprocess, matching how the rest of
soccer-cam does video work.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

# Small and short: the suite spends its time on pipeline stages, not decoding.
# Still a real MP4, because combine/trim run actual ffmpeg over these.
#
# H.265 to match what the cameras actually record. The Baichuan download path
# remuxes a raw elementary stream and defaults to H265 when the camera does not
# report a codec (reolink_download._download_and_mux_async), so an H.264
# fixture is parsed as H.265 and the remux fails with EINVAL.
WIDTH = 640
HEIGHT = 360
FPS = 15
CLIP_SECONDS = 10

# The harness asserts two groups were created. Clips land in the same group
# when they are within GROUP_GAP_SECONDS (5s) of each other, so two clips per
# group, with a gap between the groups that is comfortably larger.
CLIPS_PER_GROUP = 2
GROUP_COUNT = 2


def _frame(index: int, total: int, group: int) -> av.VideoFrame:
    """One frame: a moving bar over a per-group background.

    Deliberately not a still image — a static frame compresses to almost
    nothing and would not exercise the decoder the way a real recording does.
    """
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    # Distinct background per group, so a human eyeballing the output can tell
    # which group a combined video came from.
    img[:, :] = (30 + 40 * group, 45, 70 - 20 * group)

    # A bar sweeping left to right over the clip.
    x = int((index / max(total - 1, 1)) * (WIDTH - 60))
    img[HEIGHT // 3 : 2 * HEIGHT // 3, x : x + 60] = (240, 240, 240)

    return av.VideoFrame.from_ndarray(img, format="rgb24")


def write_clip(path: Path, group: int) -> None:
    """Write one H.265 MP4 the camera simulator can serve."""
    total = FPS * CLIP_SECONDS
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx265", rate=FPS)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"
        # Keep every clip independently decodable at its start, which is what
        # lets the pipeline concatenate them without re-encoding.
        stream.codec_context.options = {
            "g": str(FPS),
            "preset": "ultrafast",
            # Annex-B friendly: the download path pulls a raw elementary
            # stream off the camera and remuxes it.
            "x265-params": "log-level=none",
        }
        stream.time_base = Fraction(1, FPS)

        for i in range(total):
            for packet in stream.encode(_frame(i, total, group)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "test_clips",
        help="directory to write clips into (default: tests/e2e/test_clips)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite clips that are already there",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    existing = sorted(args.out.glob("*.mp4"))
    if existing and not args.force:
        print(f"{args.out} already has {len(existing)} clip(s); pass --force to redo.")
        return 0

    written = []
    for group in range(GROUP_COUNT):
        for n in range(CLIPS_PER_GROUP):
            # Named in the order the simulator should seed them. It assigns the
            # recording timestamps itself, so the names only need to sort.
            path = args.out / f"clip_g{group}_{n}.mp4"
            write_clip(path, group)
            written.append(path)
            print(f"  wrote {path.name} ({path.stat().st_size:,} bytes)")

    print(
        f"\n{len(written)} clips in {args.out} "
        f"-- {GROUP_COUNT} groups x {CLIPS_PER_GROUP}, {CLIP_SECONDS}s each."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
