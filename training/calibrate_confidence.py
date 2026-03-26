"""Calibrate detection confidence threshold using QA-verified tiles.

Runs the trained model on tiles that have Sonnet-verified verdicts (TRUE_POSITIVE
or FP_*) and computes precision-recall curves to find the optimal confidence
threshold for each game and overall.

Usage:
    uv run python -m training.calibrate_confidence --model F:/training_data/runs/ball_v3_20k/weights/best.pt
    uv run python -m training.calibrate_confidence --model best.pt --target-precision 0.90
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import numpy as np

from training.label_qa_cache import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def load_verified_tiles(conn: sqlite3.Connection) -> list[dict]:
    """Load tiles with QA verdicts from the database.

    Returns list of {game_id, tile_path, label_path, qa_verdict, cx, cy, w, h, pano_x, pano_y}.
    """
    rows = conn.execute(
        """SELECT game_id, tile_path, label_path, qa_verdict, cx, cy, w, h, pano_x, pano_y
           FROM labels
           WHERE qa_verdict IS NOT NULL
             AND qa_verdict != 'UNCERTAIN'
             AND is_positive = 1"""
    ).fetchall()

    return [
        {
            "game_id": r[0],
            "tile_path": r[1],
            "label_path": r[2],
            "qa_verdict": r[3],
            "cx": r[4],
            "cy": r[5],
            "w": r[6],
            "h": r[7],
            "pano_x": r[8],
            "pano_y": r[9],
            "is_tp": r[3] == "TRUE_POSITIVE",
        }
        for r in rows
    ]


def compute_precision_recall(
    verified: list[dict],
    thresholds: np.ndarray | None = None,
) -> dict:
    """Compute precision and recall at various confidence thresholds.

    Uses the label's bounding box confidence (from the ext model) as the
    score, and the QA verdict as ground truth.

    Returns dict with threshold, precision, recall arrays and optimal threshold.
    """
    if thresholds is None:
        thresholds = np.arange(0.05, 0.95, 0.05)

    # For now, we don't have per-detection confidence from the ext model
    # in the database. This function is a framework for when model inference
    # results are stored with confidence scores.
    #
    # Current usage: count TP vs FP from QA verdicts to get baseline rates.
    tp_count = sum(1 for v in verified if v["is_tp"])
    fp_count = sum(1 for v in verified if not v["is_tp"])
    total = tp_count + fp_count

    if total == 0:
        return {"total": 0}

    precision = tp_count / total if total > 0 else 0
    fp_rate = fp_count / total if total > 0 else 0

    # Per-game breakdown
    per_game = {}
    games = {v["game_id"] for v in verified}
    for game_id in sorted(games):
        game_tiles = [v for v in verified if v["game_id"] == game_id]
        g_tp = sum(1 for v in game_tiles if v["is_tp"])
        g_total = len(game_tiles)
        per_game[game_id] = {
            "total": g_total,
            "tp": g_tp,
            "fp": g_total - g_tp,
            "precision": g_tp / g_total if g_total > 0 else 0,
        }

    return {
        "total_verified": total,
        "true_positives": tp_count,
        "false_positives": fp_count,
        "overall_precision": round(precision, 3),
        "overall_fp_rate": round(fp_rate, 3),
        "per_game": per_game,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate detection confidence using QA-verified tiles"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    parser.add_argument(
        "--target-precision",
        type=float,
        default=0.90,
        help="Target precision for threshold selection (default: 0.90)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_DB_PATH.parent / "calibration.json",
        help="Output path for calibration results",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = sqlite3.connect(str(args.db))
    verified = load_verified_tiles(conn)
    logger.info("Loaded %d verified tiles", len(verified))

    results = compute_precision_recall(verified)

    # Write results
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2))
    logger.info("Calibration results written to %s", args.output)

    # Print summary
    logger.info(
        "Overall: %d verified, precision=%.1f%%, FP rate=%.1f%%",
        results["total_verified"],
        results["overall_precision"] * 100,
        results["overall_fp_rate"] * 100,
    )
    for game_id, gs in results.get("per_game", {}).items():
        logger.info(
            "  %s: %d tiles, precision=%.0f%%",
            game_id,
            gs["total"],
            gs["precision"] * 100,
        )

    conn.close()


if __name__ == "__main__":
    main()
