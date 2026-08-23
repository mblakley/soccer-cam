"""Stage real camera recordings as the E2E camera simulator's fixture.

``tests/e2e/test_clips/`` is gitignored, so a fresh clone mounts an empty
directory into the simulator, which then serves nothing and
``test_complete_pipeline`` ends at ``groups_created: 0/2`` no matter what the
pipeline does.

This copies real recordings out of ``shared_data/`` — the same thing
``run_simulator_test.ps1`` does with a hardcoded pair. Real files, not
synthetic ones: the download path pulls a raw H.265 elementary stream off the
camera and remuxes it, and a fabricated encode fails that remux with EINVAL
even when the codec and resolution match. The bytes have to have come from a
camera.

Run::

    uv run python -m tests.e2e.make_test_clips

Then re-seed the simulator, which caches its manifest in a docker volume and
will otherwise keep serving whatever it saw first::

    docker compose --profile reolink down -v
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# The harness asserts two groups were created. The simulator seeds what it
# finds into groups of three with a gap between them, so four clips yields the
# two groups it wants.
CLIP_COUNT = 4

# Recordings the camera actually wrote. Reolink names them
# Rec<stream>_DST<date>_<start>_<end>_..., which is also how they sort.
CLIP_GLOB = "Rec*.mp4"


def find_source_clips(search_root: Path, count: int) -> list[Path]:
    """Pick the smallest real recordings available, for a quick test run.

    Smallest rather than first: each one is downloaded over a simulated camera
    protocol during the test, so a 300 MB clip costs minutes and buys no extra
    coverage.
    """
    candidates = [
        p
        for p in search_root.rglob(CLIP_GLOB)
        # combined.mp4 / *-raw.mp4 are pipeline *outputs*, not camera files.
        if p.is_file() and not p.name.startswith("combined")
    ]
    return sorted(candidates, key=lambda p: p.stat().st_size)[:count]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument(
        "--source",
        type=Path,
        default=here.parents[1] / "shared_data",
        help="where to look for real recordings (default: shared_data/)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=here / "test_clips",
        help="directory to stage clips into (default: tests/e2e/test_clips)",
    )
    parser.add_argument("--count", type=int, default=CLIP_COUNT)
    parser.add_argument(
        "--force", action="store_true", help="restage even if clips are present"
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    if sorted(args.out.glob("*.mp4")) and not args.force:
        print(f"{args.out} already has clips; pass --force to restage.")
        return 0

    if not args.source.exists():
        print(f"No source directory at {args.source}.")
        print("Point --source at a folder holding real camera recordings.")
        return 1

    clips = find_source_clips(args.source, args.count)
    if len(clips) < args.count:
        print(
            f"Found only {len(clips)} recording(s) under {args.source}; "
            f"need {args.count}."
        )
        print("Point --source at a folder holding real camera recordings.")
        return 1

    for old in args.out.glob("*.mp4"):
        old.unlink()

    total = 0
    for clip in clips:
        shutil.copy2(clip, args.out / clip.name)
        total += clip.stat().st_size
        print(f"  staged {clip.name} ({clip.stat().st_size / 1048576:.1f} MB)")

    print(f"\n{len(clips)} clips in {args.out} ({total / 1048576:.1f} MB).")
    print("Re-seed the simulator: docker compose --profile reolink down -v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
