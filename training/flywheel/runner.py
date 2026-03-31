"""Flywheel runner — orchestrates continuous label improvement cycles.

Long-running process that manages the train→detect→fill→review loop.
State persisted to JSON so it can be killed and restarted at any step.

Usage:
    uv run python -m training.flywheel.runner
    uv run python -m training.flywheel.runner --step coverage  # run single step
    uv run python -m training.flywheel.runner --status         # show current state
    uv run python -m training.flywheel.runner --cycle          # run one full cycle
    uv run python -m training.flywheel.runner --loop           # run until 95% coverage
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from training.flywheel.coverage import measure_all_games

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STATE_PATH = Path("D:/training_data/flywheel_state.json")
LABELS_DIR = Path("D:/training_data/labels_640_ext")
TILES_DIR = Path("D:/training_data/tiles_640")
DATASET_DIR = Path("D:/training_data/ball_dataset_v3")
PENDING_LABELS_DIR = Path("D:/training_data/pending_labels")
HUMAN_LABELS_DIR = Path("D:/training_data/human_labels")
RUNS_DIR = Path("D:/training_data/runs")
COVERAGE_TARGET = 0.95
MODEL_NAME = "yolo26l.pt"  # large: training on RTX 4070 8GB only
TRAIN_EPOCHS = 50  # per cycle — resume from checkpoint each cycle


def load_state() -> dict:
    """Load flywheel state from disk."""
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "cycle": 0,
        "step": None,
        "step_index": 0,
        "started": None,
        "last_updated": None,
        "model_path": None,
        "per_game_coverage": {},
        "total_gaps": 0,
        "long_gaps": 0,
        "human_queue_size": 0,
        "history": [],
    }


def save_state(state: dict):
    """Persist flywheel state to disk."""
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def print_status(state: dict):
    """Print human-readable status."""
    print(f"Flywheel cycle: {state['cycle']}")
    print(f"Current step: {state['step']} (index {state['step_index']})")
    print(f"Last updated: {state['last_updated']}")
    print(f"Model: {state['model_path'] or 'none (will use pretrained ' + MODEL_NAME + ')'}")
    print(f"Total gaps: {state['total_gaps']} ({state['long_gaps']} long)")
    print(f"Human queue: {state['human_queue_size']} items")

    if state["per_game_coverage"]:
        coverages = list(state["per_game_coverage"].values())
        avg = sum(coverages) / len(coverages)
        below_target = sum(1 for c in coverages if c < COVERAGE_TARGET)
        print(f"Coverage: {avg:.1%} avg, {below_target}/{len(coverages)} games below {COVERAGE_TARGET:.0%}")
        print("\nPer-game:")
        for game, cov in sorted(state["per_game_coverage"].items()):
            flag = " <--" if cov < COVERAGE_TARGET else ""
            print(f"  {game}: {cov:.1%}{flag}")

    if state["history"]:
        print(f"\nHistory ({len(state['history'])} cycles):")
        for h in state["history"][-5:]:
            print(f"  Cycle {h['cycle']}: {h['coverage']:.1%}, {h['total_gaps']} gaps")


# ── Step implementations ─────────────────────────────────────────────


STEPS = [
    "coverage",       # Measure current coverage, find gaps
    "auto_fill",      # Fill short gaps automatically
    "sonnet_triage",  # Send long gaps to Sonnet
    "merge_labels",   # Merge all new labels (auto + Sonnet + human)
    "build_dataset",  # Rebuild YOLO dataset from merged labels
    "train",          # Train model (resume from checkpoint)
    "evaluate",       # Evaluate new model, measure improvement
]


def step_coverage(state: dict) -> dict:
    """Step 1: Measure coverage for all labeled games, find gaps."""
    logger.info("=== COVERAGE: Measuring all games ===")

    labels_dir = LABELS_DIR
    if not labels_dir.exists():
        labels_dir = Path("F:/training_data/labels_640_ext")
    if not labels_dir.exists():
        logger.error("No labels directory found on D: or F:")
        return state

    results = measure_all_games(labels_dir)

    # Update state
    state["per_game_coverage"] = {r["game_id"]: r["coverage"] for r in results if not r.get("error")}
    state["total_gaps"] = sum(r.get("gap_count", 0) for r in results)
    state["long_gaps"] = sum(r.get("long_gaps", 0) for r in results)

    # Save detailed gap data for next steps
    all_gaps = []
    for r in results:
        all_gaps.extend(r.get("gaps", []))
    all_gaps.sort(key=lambda g: g.get("priority", 0), reverse=True)

    gaps_path = STATE_PATH.parent / "flywheel_gaps.json"
    with open(gaps_path, "w") as f:
        json.dump(all_gaps, f, indent=2)
    logger.info("Saved %d gaps to %s", len(all_gaps), gaps_path)

    return state


def step_auto_fill(state: dict) -> dict:
    """Step 2: Automatically fill short gaps using ONNX low-conf re-detection."""
    logger.info("=== AUTO-FILL: Filling short gaps ===")

    gaps_path = STATE_PATH.parent / "flywheel_gaps.json"
    if not gaps_path.exists():
        logger.error("No gaps file found — run coverage step first")
        return state

    with open(gaps_path) as f:
        all_gaps = json.load(f)

    short_gaps = [g for g in all_gaps if g["gap_frames"] <= 50]
    long_gaps = [g for g in all_gaps if g["gap_frames"] > 50]
    logger.info("%d short gaps to auto-fill, %d long gaps for Sonnet/human", len(short_gaps), len(long_gaps))

    if not short_gaps:
        logger.info("No short gaps to fill")
        return state

    # Group gaps by game+segment for efficient processing
    from collections import defaultdict
    seg_gaps: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for gap in short_gaps:
        seg_gaps[(gap["game_id"], gap["segment"])].append(gap)

    PENDING_LABELS_DIR.mkdir(parents=True, exist_ok=True)
    filled = 0

    for (game_id, segment), gaps in seg_gaps.items():
        # For each gap, interpolate position and write a candidate label
        # This is the simplest auto-fill: linear interpolation between endpoints
        # More sophisticated methods (ONNX low-conf, frame diff) can be added later
        for gap in gaps:
            n_missing = (gap["gap_frames"] // 4) - 1  # assuming frame_interval=4
            if n_missing <= 0:
                continue

            for k in range(1, n_missing + 1):
                frac = k / (n_missing + 1)
                interp_x = gap["x_start"] + frac * (gap["x_end"] - gap["x_start"])
                interp_y = gap["y_start"] + frac * (gap["y_end"] - gap["y_start"])
                interp_fi = gap["frame_start"] + k * 4

                # Save as pending label (will be merged in step 4)
                label = {
                    "game_id": game_id,
                    "segment": segment,
                    "frame_idx": interp_fi,
                    "pano_x": round(interp_x, 1),
                    "pano_y": round(interp_y, 1),
                    "source": "interpolation",
                    "confidence": 0.5,  # lower confidence for interpolated
                    "gap_frames": gap["gap_frames"],
                    "displacement": gap["displacement"],
                }

                label_file = PENDING_LABELS_DIR / f"{game_id}_{segment}_{interp_fi:06d}.json"
                with open(label_file, "w") as f:
                    json.dump(label, f)
                filled += 1

    logger.info("Auto-filled %d gap positions via interpolation", filled)
    state["auto_filled"] = filled
    return state


def step_sonnet_triage(state: dict) -> dict:
    """Step 3: Queue long gaps for Sonnet + human review."""
    logger.info("=== SONNET TRIAGE: Processing long gaps ===")

    gaps_path = STATE_PATH.parent / "flywheel_gaps.json"
    if not gaps_path.exists():
        logger.error("No gaps file found — run coverage step first")
        return state

    with open(gaps_path) as f:
        all_gaps = json.load(f)

    long_gaps = [g for g in all_gaps if g["gap_frames"] > 50]
    long_gaps.sort(key=lambda g: g.get("priority", 0), reverse=True)
    logger.info("%d long gaps (>2s) to process", len(long_gaps))

    # Save to human queue — Sonnet triage would filter before queueing,
    # but for now all long gaps go to the queue
    human_queue_path = STATE_PATH.parent / "flywheel_human_queue.json"
    with open(human_queue_path, "w") as f:
        json.dump(long_gaps, f, indent=2)
    state["human_queue_size"] = len(long_gaps)

    logger.info("Queued %d gaps for review (priority range: %.1f - %.1f)",
                len(long_gaps),
                long_gaps[-1]["priority"] if long_gaps else 0,
                long_gaps[0]["priority"] if long_gaps else 0)
    return state


def step_merge_labels(state: dict) -> dict:
    """Step 4: Merge labels from all sources into main label set."""
    logger.info("=== MERGE: Collecting labels from all sources ===")

    from training.data_prep.trajectory_validator import STEP_X, STEP_Y, TILE_SIZE

    merged = 0

    # Collect from pending_labels/ (auto-fill) and human_labels/
    for source_dir in [PENDING_LABELS_DIR, HUMAN_LABELS_DIR]:
        if not source_dir.exists():
            continue

        for label_file in source_dir.glob("*.json"):
            with open(label_file) as f:
                label = json.load(f)

            game_id = label["game_id"]
            segment = label["segment"]
            frame_idx = label["frame_idx"]
            pano_x = label["pano_x"]
            pano_y = label["pano_y"]

            # Convert panoramic coords to tile labels
            # Find which tile(s) this falls in
            for row in range(3):
                for col in range(7):
                    tile_x_start = col * STEP_X
                    tile_y_start = row * STEP_Y
                    tile_x_end = tile_x_start + TILE_SIZE
                    tile_y_end = tile_y_start + TILE_SIZE

                    if tile_x_start <= pano_x < tile_x_end and tile_y_start <= pano_y < tile_y_end:
                        # Convert to normalized tile coords
                        cx_norm = (pano_x - tile_x_start) / TILE_SIZE
                        cy_norm = (pano_y - tile_y_start) / TILE_SIZE
                        # Approximate ball size in tile (normalized)
                        w_norm = 0.025  # ~16px ball in 640px tile
                        h_norm = 0.025

                        # Write YOLO label file
                        tile_stem = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
                        label_dir = LABELS_DIR / game_id
                        label_dir.mkdir(parents=True, exist_ok=True)
                        label_path = label_dir / f"{tile_stem}.txt"

                        # Append if exists (don't overwrite existing detections)
                        with open(label_path, "a") as lf:
                            lf.write(f"0 {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}\n")
                        merged += 1

    # Clean up processed pending labels
    if PENDING_LABELS_DIR.exists():
        processed = list(PENDING_LABELS_DIR.glob("*.json"))
        for f in processed:
            f.unlink()
        logger.info("Cleaned %d processed pending labels", len(processed))

    logger.info("Merged %d new labels into %s", merged, LABELS_DIR)
    state["merged_labels"] = merged
    return state


def step_build_dataset(state: dict) -> dict:
    """Step 5: Rebuild YOLO dataset from current labels."""
    logger.info("=== BUILD: Rebuilding dataset ===")

    from training.data_prep.organize_dataset import organize_dataset

    # Clear old dataset
    if DATASET_DIR.exists():
        shutil.rmtree(DATASET_DIR)

    tiles_dir = TILES_DIR
    if not tiles_dir.exists():
        tiles_dir = Path("F:/training_data/tiles_640")

    labels_dir = LABELS_DIR
    if not labels_dir.exists():
        labels_dir = Path("F:/training_data/labels_640_ext")

    result = organize_dataset(
        tiles_dir=tiles_dir,
        labels_dir=labels_dir,
        output_dir=DATASET_DIR,
        val_split=0.15,
        exclude_rows=set(),  # Include r0 for v3
    )

    logger.info(
        "Dataset built: %d train, %d val (%d/%d labeled)",
        result.get("train_images", 0),
        result.get("val_images", 0),
        result.get("train_labeled", 0),
        result.get("val_labeled", 0),
    )

    # Write YAML config
    yaml_path = DATASET_DIR / "dataset.yaml"
    yaml_path.write_text(
        f"path: {DATASET_DIR}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"nc: 3\n"
        f"names:\n"
        f"  0: game_ball\n"
        f"  1: static_ball\n"
        f"  2: not_ball\n"
    )
    state["dataset_yaml"] = str(yaml_path)
    state["dataset_stats"] = result
    return state


def step_train(state: dict) -> dict:
    """Step 6: Train YOLO26 model."""
    logger.info("=== TRAIN: Starting YOLO26 training ===")

    yaml_path = state.get("dataset_yaml")
    if not yaml_path or not Path(yaml_path).exists():
        logger.error("No dataset YAML found — run build_dataset step first")
        return state

    model_path = state.get("model_path")
    run_name = f"v3_cycle{state['cycle']}"

    # Build training command
    if model_path and Path(model_path).exists():
        # Resume from previous cycle's best weights
        cmd = [
            sys.executable, "-c",
            f"from ultralytics import YOLO; "
            f"model = YOLO('{model_path}'); "
            f"model.train(data='{yaml_path}', epochs={TRAIN_EPOCHS}, "
            f"imgsz=640, batch=8, workers=0, device=0, "
            f"project='{RUNS_DIR}', name='{run_name}', "
            f"resume=False, pretrained=True)"
        ]
    else:
        # First cycle: start from pretrained YOLO26
        cmd = [
            sys.executable, "-c",
            f"from ultralytics import YOLO; "
            f"model = YOLO('{MODEL_NAME}'); "
            f"model.train(data='{yaml_path}', epochs={TRAIN_EPOCHS}, "
            f"imgsz=640, batch=8, workers=0, device=0, "
            f"project='{RUNS_DIR}', name='{run_name}')"
        ]

    logger.info("Running: %s", run_name)
    result = subprocess.run(cmd, capture_output=False, text=True)

    if result.returncode != 0:
        logger.error("Training failed with exit code %d", result.returncode)
        return state

    # Find best weights from this run
    best_weights = RUNS_DIR / run_name / "weights" / "best.pt"
    if best_weights.exists():
        state["model_path"] = str(best_weights)
        logger.info("Best weights: %s", best_weights)
    else:
        # Try last.pt
        last_weights = RUNS_DIR / run_name / "weights" / "last.pt"
        if last_weights.exists():
            state["model_path"] = str(last_weights)
            logger.info("Last weights (no best): %s", last_weights)
        else:
            logger.error("No weights found after training")

    return state


def step_evaluate(state: dict) -> dict:
    """Step 7: Evaluate model and record improvement."""
    logger.info("=== EVALUATE: Measuring improvement ===")

    avg_coverage = sum(state["per_game_coverage"].values()) / max(len(state["per_game_coverage"]), 1)

    state["history"].append({
        "cycle": state["cycle"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coverage": avg_coverage,
        "total_gaps": state["total_gaps"],
        "long_gaps": state["long_gaps"],
        "human_queue_size": state["human_queue_size"],
        "model_path": state["model_path"],
        "auto_filled": state.get("auto_filled", 0),
        "merged_labels": state.get("merged_labels", 0),
    })

    logger.info(
        "Cycle %d complete: %.1f%% coverage, %d gaps (%d long), %d in human queue",
        state["cycle"], avg_coverage * 100,
        state["total_gaps"], state["long_gaps"], state["human_queue_size"],
    )

    if state.get("model_path"):
        logger.info("Model: %s", state["model_path"])

    return state


STEP_FUNCTIONS = {
    "coverage": step_coverage,
    "auto_fill": step_auto_fill,
    "sonnet_triage": step_sonnet_triage,
    "merge_labels": step_merge_labels,
    "build_dataset": step_build_dataset,
    "train": step_train,
    "evaluate": step_evaluate,
}


# ── Cycle management ─────────────────────────────────────────────────


def run_cycle(state: dict) -> dict:
    """Run one full cycle through all steps."""
    state["cycle"] += 1
    state["started"] = datetime.now(timezone.utc).isoformat()
    logger.info("=== Starting cycle %d ===", state["cycle"])

    for i, step_name in enumerate(STEPS):
        if i < state.get("step_index", 0):
            logger.info("Skipping %s (already done)", step_name)
            continue

        state["step"] = step_name
        state["step_index"] = i
        save_state(state)

        step_fn = STEP_FUNCTIONS[step_name]
        start = time.time()
        state = step_fn(state)
        elapsed = time.time() - start
        logger.info("Step %s completed in %.0fs", step_name, elapsed)

    state["step"] = "done"
    state["step_index"] = len(STEPS)
    save_state(state)
    return state


def run_loop(state: dict, max_cycles: int = 10):
    """Run cycles until coverage target reached or max cycles hit."""
    for _ in range(max_cycles):
        state = run_cycle(state)
        save_state(state)

        avg_coverage = sum(state["per_game_coverage"].values()) / max(len(state["per_game_coverage"]), 1)
        if avg_coverage >= COVERAGE_TARGET:
            logger.info("Target reached: %.1f%% >= %.0f%%", avg_coverage * 100, COVERAGE_TARGET * 100)
            break

        state["step_index"] = 0
        logger.info("Coverage %.1f%% < %.0f%% — starting next cycle", avg_coverage * 100, COVERAGE_TARGET * 100)


def main():
    parser = argparse.ArgumentParser(description="Flywheel: continuous label improvement")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--step", choices=STEPS, help="Run a single step")
    parser.add_argument("--cycle", action="store_true", help="Run one full cycle")
    parser.add_argument("--loop", action="store_true", help="Run cycles until target coverage")
    parser.add_argument("--max-cycles", type=int, default=10, help="Max cycles for --loop")
    args = parser.parse_args()

    state = load_state()

    if args.status:
        print_status(state)
        return

    if args.step:
        state["step"] = args.step
        step_fn = STEP_FUNCTIONS[args.step]
        state = step_fn(state)
        save_state(state)
        return

    if args.cycle:
        state["step_index"] = 0
        state = run_cycle(state)
        save_state(state)
        return

    if args.loop:
        state["step_index"] = 0
        run_loop(state, max_cycles=args.max_cycles)
        return

    print_status(state)
    print("\nUsage:")
    print("  --status      Show current state")
    print("  --step X      Run a single step (coverage, auto_fill, etc.)")
    print("  --cycle       Run one full cycle")
    print("  --loop        Run cycles until 95% coverage")


if __name__ == "__main__":
    main()
