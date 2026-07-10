"""Run the production homegrown pipeline chain on a video — the E2E harness.

Drives the REGISTERED product steps (ball_detect -> ball_select -> plan_camera ->
render) through a real manifest, exactly as the shipped pipeline runner would:
one path, no harness-only code in the middle. Use it to verify the chain on a
game segment before/after a model or step change.

The input video is COPIED into ``--work-dir`` first (artifacts land next to the
input, and the archive game dirs on F: must stay read-only), following the
pull-local-process-push rule.

    python -m training.cli.run_pipeline_chain \
      --input "F:/Heat_2012s/<game>/<segment>.mp4" \
      --polygon-from-game-json "F:/Heat_2012s/<game>/game.json" \
      --detector-onnx G:/ballresearch/selector/models/ball_detector_hn2.onnx \
      --selector-npz G:/ballresearch/selector/models/selector_v5.npz \
      --work-dir G:/ballresearch/selector/e2e/spc_seg6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path


async def _run(args) -> None:
    import video_grouper.pipeline.register_steps  # noqa: F401
    from video_grouper.pipeline import create_step
    from video_grouper.pipeline.base import StepContext
    from video_grouper.pipeline.manifest import PipelineManifest

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    src = Path(args.input)
    local_in = work / src.name
    if not local_in.exists() or local_in.stat().st_size != src.stat().st_size:
        print(f"copying {src} -> {local_in}", flush=True)
        shutil.copy2(src, local_in)

    if args.polygon_from_game_json:
        gj = json.loads(
            Path(args.polygon_from_game_json).read_text(
                encoding="utf-8", errors="ignore"
            )
        )
        polygon = gj["field_polygon"]
    else:
        polygon = json.loads(Path(args.polygon).read_text(encoding="utf-8"))["polygon"]
    field_file = work / "field_polygon.json"
    field_file.write_text(json.dumps({"polygon": polygon}))

    out_path = work / args.out_name
    manifest = PipelineManifest.load_or_init(work, str(local_in), str(out_path))
    manifest.put("field_polygon_path", str(field_file))
    ctx = StepContext(group_dir=work, team_name=None, storage_path=work)

    chain: list[tuple[str, dict]] = [
        (
            "ball_detect",
            {
                "model_path": args.detector_onnx,
                "detect_frame_interval": args.stride,
                "detect_target_width": args.target_width,
            },
        ),
        ("ball_select", {"select_model_path": args.selector_npz}),
        ("plan_camera", {}),
        ("render", {}),
    ]
    for name, cfg in chain:
        step = create_step(name, cfg)
        t0 = time.time()
        print(f"=== {name} ===", flush=True)
        ok = await step.run(manifest, ctx)
        print(f"=== {name}: ok={ok} in {time.time() - t0:.0f}s ===", flush=True)
        if not ok:
            raise SystemExit(f"{name} returned False")
    print(f"chain complete -> {out_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="source video (copied to work dir)")
    poly = ap.add_mutually_exclusive_group(required=True)
    poly.add_argument("--polygon", help='json file with {"polygon": [[x,y]...]}')
    poly.add_argument(
        "--polygon-from-game-json", help="game.json to read field_polygon from"
    )
    ap.add_argument("--detector-onnx", required=True)
    ap.add_argument("--selector-npz", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out-name", default="broadcast.mp4")
    ap.add_argument("--stride", type=int, default=8)
    ap.add_argument("--target-width", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
