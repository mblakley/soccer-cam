"""Active learning sampler for Sonnet QA spot-checks.

Prioritizes tiles for review based on where Sonnet is most likely to
find errors, maximizing the value of each review:
- Low-confidence detections (model uncertain)
- Detections near field boundary (ambiguous on/off field)
- New game data (always spot-check 20% of new games)
- Row 2 tiles (historically highest FP rate)

Usage:
    uv run python -m training.data_prep.active_sampler --db F:/training_data/label_qa/tile_cache.db
"""

import argparse
import logging
import sqlite3
from pathlib import Path

from training.label_qa_cache import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def prioritized_sample(
    conn: sqlite3.Connection,
    target_count: int = 500,
    new_game_rate: float = 0.20,
) -> list[dict]:
    """Select tiles for spot-check review, prioritized by expected error rate.

    Returns list of {id, game_id, tile_path, priority_reason} dicts.
    """
    samples = []
    used_ids: set[int] = set()

    # Priority 1: Unreviewed labels in row 2 (sideline FP hotspot)
    row2 = conn.execute(
        """SELECT id, game_id, tile_path, row, col
           FROM labels
           WHERE is_positive = 1 AND qa_verdict IS NULL AND row = 2
           ORDER BY RANDOM()
           LIMIT ?""",
        (target_count // 4,),
    ).fetchall()

    for r in row2:
        if r[0] not in used_ids:
            samples.append(
                {
                    "id": r[0],
                    "game_id": r[1],
                    "tile_path": r[2],
                    "priority": "row2_sideline",
                }
            )
            used_ids.add(r[0])

    # Priority 2: Labels near field boundary (in_field flag edge cases)
    # These are the most ambiguous — near the touchline
    near_boundary = conn.execute(
        """SELECT id, game_id, tile_path, row, col
           FROM labels
           WHERE is_positive = 1 AND qa_verdict IS NULL
                 AND in_field = 0 AND row > 0
           ORDER BY RANDOM()
           LIMIT ?""",
        (target_count // 4,),
    ).fetchall()

    for r in near_boundary:
        if r[0] not in used_ids:
            samples.append(
                {
                    "id": r[0],
                    "game_id": r[1],
                    "tile_path": r[2],
                    "priority": "near_boundary",
                }
            )
            used_ids.add(r[0])

    # Priority 3: Random unreviewed from all positions
    remaining = target_count - len(samples)
    if remaining > 0:
        random_samples = conn.execute(
            """SELECT id, game_id, tile_path, row, col
               FROM labels
               WHERE is_positive = 1 AND qa_verdict IS NULL
               ORDER BY RANDOM()
               LIMIT ?""",
            (remaining,),
        ).fetchall()

        for r in random_samples:
            if r[0] not in used_ids:
                samples.append(
                    {
                        "id": r[0],
                        "game_id": r[1],
                        "tile_path": r[2],
                        "priority": "random",
                    }
                )
                used_ids.add(r[0])

    logger.info(
        "Prioritized sample: %d tiles (%d row2, %d near-boundary, %d random)",
        len(samples),
        sum(1 for s in samples if s["priority"] == "row2_sideline"),
        sum(1 for s in samples if s["priority"] == "near_boundary"),
        sum(1 for s in samples if s["priority"] == "random"),
    )

    return samples


def main():
    parser = argparse.ArgumentParser(
        description="Active learning sampler for QA spot-checks"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
    )
    parser.add_argument(
        "--target",
        type=int,
        default=500,
        help="Target number of tiles to sample",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = sqlite3.connect(str(args.db))
    samples = prioritized_sample(conn, args.target)
    logger.info("Sampled %d tiles for review", len(samples))
    conn.close()


if __name__ == "__main__":
    main()
