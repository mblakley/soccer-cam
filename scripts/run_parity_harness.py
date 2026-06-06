"""iOS-port parity harness — run detect → track → render deterministically.

Produces the reference outputs the iOS Swift port (Phase 0–5 of the iOS port
plan) compares its own output against:

- ``detections.json`` — per-frame ball detections (E0.A1/A4 baseline)
- ``trajectory.json`` — Kalman-smoothed trajectory (E0.A1 baseline)
- ``leveled_pano_map_x.npy`` / ``leveled_pano_map_y.npy`` — constant world-up
  panorama maps (E0.B1/B2 baseline)
- ``camera_states.json`` — per-frame {yaw, pitch, zoom, hfov} from the camera
  state machine (E0.B4 baseline)
- ``render_frame_NNNNNN.png`` — sampled rendered frames every 30 frames
  (E0.B3/C1 baseline)
- ``output.mp4`` — full broadcast-quality rendered video (E0.C1 baseline)

Two re-runs over the same input produce byte-identical detections.json,
trajectory.json, and camera_states.json (the harness forces CPU EP + sequential
single-thread ONNX execution + sorted JSON output). The rendered mp4 is NOT
byte-identical run-to-run because libx264 encoding has internal nondeterminism
that we accept — the iOS port's visual parity check (E0.B3/C1) is per-frame
PNG comparison, not file-level mp4 hash.

Usage:
    python scripts/run_parity_harness.py \\
        --input-video /path/to/panorama.mp4 \\
        --model /path/to/ball_detector.onnx \\
        --output-dir /path/to/parity_run/

Optional:
    --field-polygon /path/to/polygon.json
    --frame-interval 4
    --render-mode broadcast|coach
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

# Repo root on sys.path so we can `from video_grouper...` when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_grouper.inference.determinism import seed_everything  # noqa: E402
from video_grouper.pipeline import register_steps  # noqa: E402,F401
from video_grouper.pipeline.base import StepContext, StepSpec  # noqa: E402
from video_grouper.pipeline.runner import PipelineRunner  # noqa: E402

logger = logging.getLogger(__name__)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard-link src→dst (cheap), fall back to copy across filesystems."""
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


async def _run_harness(args: argparse.Namespace) -> int:
    input_video = Path(args.input_video).resolve()
    model_path = Path(args.model).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_video.is_file():
        logger.error("input video not found: %s", input_video)
        return 2
    if not model_path.is_file():
        logger.error("model not found: %s", model_path)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    # Place the input next to where detect+track will write their JSONs, so the
    # whole run is self-contained in output_dir and not scattered across the
    # user's filesystem next to the source video.
    staged_input = output_dir / "source.mp4"
    _link_or_copy(input_video, staged_input)

    field_polygon_path: str | None = None
    if args.field_polygon:
        polygon_src = Path(args.field_polygon).resolve()
        if not polygon_src.is_file():
            logger.error("field polygon not found: %s", polygon_src)
            return 2
        staged_polygon = output_dir / "field_polygon.json"
        _link_or_copy(polygon_src, staged_polygon)
        field_polygon_path = str(staged_polygon)

    output_mp4 = output_dir / "output.mp4"
    dump_dir = output_dir / "parity"
    dump_dir.mkdir(exist_ok=True)

    seed_everything(args.seed)

    specs = [
        StepSpec(
            step_id="detect",
            type="detect",
            config={
                "model_path": str(model_path),
                "device": "cpu",  # CPU EP is the parity baseline
                "detect_confidence": args.detect_confidence,
                "detect_frame_interval": args.frame_interval,
            },
        ),
        StepSpec(
            step_id="track",
            type="track",
            config={},
        ),
        StepSpec(
            step_id="render",
            type="render",
            config={
                "render_mode": args.render_mode,
                "render_backend": "cv2",  # cv2 path so renders are reproducible
            },
        ),
    ]

    ctx = StepContext(
        group_dir=output_dir,
        team_name=None,
        storage_path=output_dir,
        ttt_config=None,
        dump_intermediates_dir=dump_dir,
    )

    # Seed the manifest with the field polygon path so render picks it up.
    # PipelineRunner.run() loads/inits the manifest from input_path + output_path
    # and then runs steps in order — we pre-write field_polygon_path by calling
    # the load_or_init + put before calling run().
    if field_polygon_path:
        from video_grouper.pipeline.manifest import PipelineManifest

        m = PipelineManifest.load_or_init(
            output_dir, str(staged_input), str(output_mp4)
        )
        m.put("field_polygon_path", field_polygon_path)
        m.save()

    runner = PipelineRunner(specs, runtime="service")
    result = await runner.run(str(staged_input), str(output_mp4), ctx)

    if result.ok:
        logger.info(
            "parity harness complete; baselines in %s, rendered output %s",
            dump_dir,
            output_mp4,
        )
        return 0
    logger.error(
        "parity harness failed at step %s: %s", result.failed_step, result.error
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="iOS-port parity harness for soccer-cam detect/track/render"
    )
    parser.add_argument("--input-video", required=True, help="Source panorama mp4")
    parser.add_argument(
        "--model", required=True, help="Community/BYO ball-detector .onnx"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Where baselines + render output land (created if missing)",
    )
    parser.add_argument(
        "--field-polygon",
        default=None,
        help="Optional field polygon JSON (unlocks auto-leveling)",
    )
    parser.add_argument("--frame-interval", type=int, default=4)
    parser.add_argument("--detect-confidence", type=float, default=0.45)
    parser.add_argument(
        "--render-mode", choices=("broadcast", "coach"), default="broadcast"
    )
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    return asyncio.run(_run_harness(args))


if __name__ == "__main__":
    sys.exit(main())
