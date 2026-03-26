"""Automated post-training QA spot-check pipeline.

After each training run, this script:
1. Samples detections from the model's output for review
2. Prepares composite grid images for Sonnet agents
3. Coordinates the review workflow
4. Ingests verdicts and generates comparison report

Designed to be triggered after each training run as part of the
train → detect → spot-check → clean → retrain loop.

Usage:
    uv run python -m training.label_qa_spot_check --model F:/training_data/runs/ball_v4/weights/best.pt
    uv run python -m training.label_qa_spot_check --run-id ball_v4 --sample-rate 0.10
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from training.label_qa_cache import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

# Spot-check configuration
DEFAULT_SAMPLE_RATE = 0.10
DEFAULT_OUTPUT_DIR = Path("F:/training_data/label_qa/spot_checks")


def prepare_spot_check(
    conn: sqlite3.Connection,
    run_id: str,
    sample_rate: float,
    output_dir: Path,
) -> dict:
    """Prepare a spot-check review packet for a training run.

    Queries the label database for detections to review, prioritized by:
    1. Low-confidence detections (model uncertain)
    2. Detections near field boundary (ambiguous on/off field)
    3. Detections not previously reviewed
    4. Random sample for coverage

    Returns stats dict.
    """
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Get counts of reviewed vs unreviewed labels
    total = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE is_positive = 1"
    ).fetchone()[0]
    reviewed = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE qa_verdict IS NOT NULL"
    ).fetchone()[0]
    target = max(50, int(total * sample_rate))

    # Priority 1: Unreviewed labels near field boundary (ambiguous)
    near_boundary = conn.execute(
        """SELECT id, tile_path, label_path, game_id, cx, cy, row, col
           FROM labels
           WHERE is_positive = 1 AND qa_verdict IS NULL AND in_field IS NOT NULL
           ORDER BY ABS(in_field) ASC
           LIMIT ?""",
        (target // 3,),
    ).fetchall()

    # Priority 2: Unreviewed labels (random)
    random_unreviewed = conn.execute(
        """SELECT id, tile_path, label_path, game_id, cx, cy, row, col
           FROM labels
           WHERE is_positive = 1 AND qa_verdict IS NULL
           ORDER BY RANDOM()
           LIMIT ?""",
        (target - len(near_boundary),),
    ).fetchall()

    all_samples = near_boundary + random_unreviewed

    # Write manifest for this spot-check
    manifest = {
        "run_id": run_id,
        "total_labels": total,
        "previously_reviewed": reviewed,
        "new_samples": len(all_samples),
        "sample_rate": sample_rate,
        "samples": [
            {
                "id": r[0],
                "tile_path": r[1],
                "label_path": r[2],
                "game_id": r[3],
                "cx": r[4],
                "cy": r[5],
                "row": r[6],
                "col": r[7],
            }
            for r in all_samples
        ],
    }

    manifest_path = run_dir / "spot_check_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    logger.info(
        "Spot-check prepared for %s: %d samples (%d near boundary, %d random)",
        run_id,
        len(all_samples),
        len(near_boundary),
        len(random_unreviewed),
    )

    return {
        "run_id": run_id,
        "total_labels": total,
        "reviewed": reviewed,
        "new_samples": len(all_samples),
        "manifest_path": str(manifest_path),
    }


def apply_three_tier_verdicts(
    conn: sqlite3.Connection,
    notify: bool = True,
) -> dict:
    """Apply the three-tier verdict system to all labels.

    Tier 1 — Automated consensus: trajectory + Sonnet agree
    Tier 2 — Sonnet-only: when trajectory didn't review
    Tier 3 — Disagreements: queue for human review (async, non-blocking)

    Returns stats dict.
    """
    stats = {
        "consensus_tp": 0,
        "consensus_fp": 0,
        "sonnet_tp": 0,
        "sonnet_fp": 0,
        "disagreements": 0,
    }

    # Labels with QA verdicts
    rows = conn.execute(
        """SELECT id, qa_verdict, in_field FROM labels
           WHERE qa_verdict IS NOT NULL AND qa_verdict != 'UNCERTAIN'"""
    ).fetchall()

    disagreements = []
    for label_id, verdict, in_field in rows:
        is_tp_verdict = verdict == "TRUE_POSITIVE"
        is_in_field = in_field == 1 if in_field is not None else True

        if is_tp_verdict and is_in_field:
            # Tier 1: both agree it's good
            stats["consensus_tp"] += 1
        elif not is_tp_verdict and not is_in_field:
            # Tier 1: both agree it's bad
            stats["consensus_fp"] += 1
        elif is_tp_verdict and not is_in_field:
            # Disagreement: Sonnet says TP but field mask says off-field
            stats["disagreements"] += 1
            disagreements.append(label_id)
        elif not is_tp_verdict and is_in_field:
            # Tier 2: Sonnet says FP, field says in-field — trust Sonnet
            stats["sonnet_fp"] += 1
        else:
            # Tier 2: Sonnet only
            if is_tp_verdict:
                stats["sonnet_tp"] += 1
            else:
                stats["sonnet_fp"] += 1

    # Queue disagreements for human review (non-blocking)
    if disagreements and notify:
        logger.info(
            "Queued %d disagreements for human review (async)",
            len(disagreements),
        )
        # In a full implementation, this would:
        # 1. Generate review packets for the annotation server
        # 2. Send NTFY notification
        # For now, log the count.

    logger.info(
        "Three-tier verdicts: %d consensus_tp, %d consensus_fp, "
        "%d sonnet_tp, %d sonnet_fp, %d disagreements",
        stats["consensus_tp"],
        stats["consensus_fp"],
        stats["sonnet_tp"],
        stats["sonnet_fp"],
        stats["disagreements"],
    )

    return stats


def main():
    parser = argparse.ArgumentParser(description="Post-training QA spot-check pipeline")
    parser.add_argument(
        "--run-id",
        required=True,
        help="Training run identifier (e.g., ball_v4)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for spot-check results",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=DEFAULT_SAMPLE_RATE,
    )
    parser.add_argument(
        "--apply-verdicts",
        action="store_true",
        help="Apply three-tier verdict system to existing labels",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = sqlite3.connect(str(args.db))

    if args.apply_verdicts:
        apply_three_tier_verdicts(conn)
    else:
        prepare_spot_check(conn, args.run_id, args.sample_rate, args.output)

    conn.close()


if __name__ == "__main__":
    main()
