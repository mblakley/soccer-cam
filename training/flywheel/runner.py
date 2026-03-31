"""Flywheel runner — orchestrates continuous label improvement cycles.

Long-running process that manages the train→detect→fill→review loop.
State persisted to JSON so it can be killed and restarted at any step.

Usage:
    uv run python -m training.flywheel.runner
    uv run python -m training.flywheel.runner --step coverage  # run single step
    uv run python -m training.flywheel.runner --status         # show current state
"""

import argparse
import json
import logging
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
COVERAGE_TARGET = 0.95

STEPS = [
    "coverage",       # Measure current coverage, find gaps
    "auto_fill",      # Fill short gaps automatically
    "sonnet_triage",  # Send long gaps to Sonnet
    "merge_labels",   # Merge all new labels (auto + Sonnet + human)
    "build_dataset",  # Rebuild YOLO dataset from merged labels
    "train",          # Train model (resume from checkpoint)
    "evaluate",       # Evaluate new model, measure improvement
]


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
    print(f"Model: {state['model_path'] or 'none'}")
    print(f"Total gaps: {state['total_gaps']} ({state['long_gaps']} long)")
    print(f"Human queue: {state['human_queue_size']} items")

    if state["per_game_coverage"]:
        coverages = list(state["per_game_coverage"].values())
        avg = sum(coverages) / len(coverages)
        below_target = sum(1 for c in coverages if c < COVERAGE_TARGET)
        print(f"Coverage: {avg:.1%} avg, {below_target}/{len(coverages)} games below {COVERAGE_TARGET:.0%}")

    if state["history"]:
        print(f"\nHistory ({len(state['history'])} cycles):")
        for h in state["history"][-5:]:
            print(f"  Cycle {h['cycle']}: {h['coverage']:.1%} → {h.get('new_coverage', '?')}")


def step_coverage(state: dict) -> dict:
    """Step 1: Measure coverage for all labeled games, find gaps."""
    logger.info("=== COVERAGE: Measuring all games ===")

    # Find all games that have labels
    if not LABELS_DIR.exists():
        logger.warning("Labels directory not found: %s", LABELS_DIR)
        # Fall back to F: drive
        labels_dir = Path("F:/training_data/labels_640_ext")
    else:
        labels_dir = LABELS_DIR

    games = sorted(d.name for d in labels_dir.iterdir() if d.is_dir())
    if not games:
        logger.error("No labeled games found")
        return state

    results = measure_all_games(labels_dir, games)

    # Update state
    state["per_game_coverage"] = {r["game_id"]: r["coverage"] for r in results}
    state["total_gaps"] = sum(r["gap_count"] for r in results)
    state["long_gaps"] = sum(r["long_gaps"] for r in results)

    # Save detailed gap data for next steps
    all_gaps = []
    for r in results:
        all_gaps.extend(r.get("gaps", []))

    # Sort by priority (highest first)
    all_gaps.sort(key=lambda g: g.get("priority", 0), reverse=True)

    gaps_path = STATE_PATH.parent / "flywheel_gaps.json"
    with open(gaps_path, "w") as f:
        json.dump(all_gaps, f, indent=2)
    logger.info("Saved %d gaps to %s", len(all_gaps), gaps_path)

    # Check if we've hit target
    avg_coverage = sum(state["per_game_coverage"].values()) / max(len(state["per_game_coverage"]), 1)
    if avg_coverage >= COVERAGE_TARGET:
        logger.info("Target coverage reached: %.1f%% >= %.0f%%", avg_coverage * 100, COVERAGE_TARGET * 100)

    return state


def step_auto_fill(state: dict) -> dict:
    """Step 2: Automatically fill short gaps (< 2 seconds)."""
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

    # TODO: Implement automated gap filling
    # For each short gap:
    #   1. Run ONNX at lower confidence (0.20) on frames within gap
    #   2. Run frame differencing at predicted positions
    #   3. Run optical flow from last known position
    #   4. If candidate found, add to pending_labels/

    logger.warning("Auto-fill not yet implemented — skipping")
    return state


def step_sonnet_triage(state: dict) -> dict:
    """Step 3: Send long gaps to Sonnet Vision for review."""
    logger.info("=== SONNET TRIAGE: Reviewing long gaps ===")

    gaps_path = STATE_PATH.parent / "flywheel_gaps.json"
    if not gaps_path.exists():
        logger.error("No gaps file found — run coverage step first")
        return state

    with open(gaps_path) as f:
        all_gaps = json.load(f)

    long_gaps = [g for g in all_gaps if g["gap_frames"] > 50]
    long_gaps.sort(key=lambda g: g.get("priority", 0), reverse=True)

    logger.info("%d long gaps to send to Sonnet (sorted by priority)", len(long_gaps))

    # TODO: Implement Sonnet triage
    # For each long gap:
    #   1. Extract panoramic frame at gap midpoint
    #   2. Crop predicted region + context
    #   3. Send to Sonnet: "Is there a soccer ball in this region?"
    #   4. If found: add label to pending_labels/
    #   5. If not found: queue for human review with high priority

    # Save human queue
    human_queue_path = STATE_PATH.parent / "flywheel_human_queue.json"
    # For now, all long gaps go to human queue
    with open(human_queue_path, "w") as f:
        json.dump(long_gaps, f, indent=2)
    state["human_queue_size"] = len(long_gaps)

    logger.warning("Sonnet triage not yet implemented — all long gaps queued for human")
    return state


def step_merge_labels(state: dict) -> dict:
    """Step 4: Merge labels from all sources into main label set."""
    logger.info("=== MERGE: Collecting labels from all sources ===")

    # TODO: Implement label merger
    # Sources:
    #   1. Auto-filled labels (from step 2)
    #   2. Sonnet-verified labels (from step 3)
    #   3. Human-reviewed labels (from annotation app, async)
    # Merge into labels_dir without duplicates

    logger.warning("Label merger not yet implemented — skipping")
    return state


def step_build_dataset(state: dict) -> dict:
    """Step 5: Rebuild YOLO dataset from current labels."""
    logger.info("=== BUILD: Rebuilding dataset ===")

    # TODO: Call organize_dataset.py with current labels
    # This creates the train/val split with hardlinks

    logger.warning("Dataset build not yet implemented — skipping")
    return state


def step_train(state: dict) -> dict:
    """Step 6: Train model (resume from checkpoint if available)."""
    logger.info("=== TRAIN: Starting/resuming training ===")

    # TODO: Launch YOLO training
    # If state['model_path'] exists, resume from that checkpoint
    # Otherwise start fresh

    logger.warning("Training step not yet implemented — skipping")
    return state


def step_evaluate(state: dict) -> dict:
    """Step 7: Evaluate model and record improvement."""
    logger.info("=== EVALUATE: Measuring improvement ===")

    # Record this cycle's results in history
    avg_coverage = sum(state["per_game_coverage"].values()) / max(len(state["per_game_coverage"]), 1)

    state["history"].append({
        "cycle": state["cycle"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coverage": avg_coverage,
        "total_gaps": state["total_gaps"],
        "long_gaps": state["long_gaps"],
        "human_queue_size": state["human_queue_size"],
        "model_path": state["model_path"],
    })

    logger.info(
        "Cycle %d complete: %.1f%% coverage, %d gaps (%d long), %d in human queue",
        state["cycle"], avg_coverage * 100,
        state["total_gaps"], state["long_gaps"], state["human_queue_size"],
    )

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


def run_cycle(state: dict) -> dict:
    """Run one full cycle through all steps."""
    state["cycle"] += 1
    state["started"] = datetime.now(timezone.utc).isoformat()
    logger.info("=== Starting cycle %d ===", state["cycle"])

    for i, step_name in enumerate(STEPS):
        # Skip steps we've already completed in this cycle (for restart)
        if i < state.get("step_index", 0):
            logger.info("Skipping %s (already done)", step_name)
            continue

        state["step"] = step_name
        state["step_index"] = i
        save_state(state)

        step_fn = STEP_FUNCTIONS[step_name]
        state = step_fn(state)

    # Cycle complete
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

        # Reset step index for next cycle
        state["step_index"] = 0

        logger.info("Coverage %.1f%% < %.0f%% target — starting next cycle", avg_coverage * 100, COVERAGE_TARGET * 100)


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

    # Default: show status
    print_status(state)
    print("\nUsage:")
    print("  --status      Show current state")
    print("  --step X      Run a single step (coverage, auto_fill, etc.)")
    print("  --cycle       Run one full cycle")
    print("  --loop        Run cycles until 95% coverage")


if __name__ == "__main__":
    main()
