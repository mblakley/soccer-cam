"""Ingest QA verdicts from Sonnet agent reviews into the label pipeline.

Reads *_result.json files from label_qa batch directories, traces each verdict
back to the source label file via batch metadata, and updates the SQLite cache
with per-label verdicts. Labels marked as false positives can then be excluded
or downweighted in the next training run.

Usage:
    uv run python -m training.data_prep.qa_verdict_ingester
    uv run python -m training.data_prep.qa_verdict_ingester --games heat__05.31.2024_vs_Fairport_home
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from training.label_qa_cache import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

# Verdict categories from Sonnet agent reviews
FP_VERDICTS = {"FP_NOT_BALL", "FP_OFF_FIELD", "FP_NOT_GAME_BALL"}
TP_VERDICTS = {"TRUE_POSITIVE"}
NEGATIVE_VERDICTS = {"TRUE_NEGATIVE", "FALSE_NEGATIVE"}


def ingest_batch_results(
    batch_dir: Path,
    batch_type: str,
) -> list[dict]:
    """Read all *_result.json and matching *.json metadata from a batch directory.

    Returns list of {tile_id, verdict, tile_meta} for tiles that have
    both a verdict and metadata with a traceable label file.
    """
    results = []

    for result_file in sorted(batch_dir.glob("*_result.json")):
        batch_id = result_file.stem.replace("_result", "")
        try:
            verdicts = json.loads(result_file.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Bad result file %s: %s", result_file, e)
            continue

        meta_file = batch_dir / f"{batch_id}.json"
        if not meta_file.exists():
            logger.warning("No metadata for %s", batch_id)
            continue

        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("Bad metadata file %s", meta_file)
            continue

        tiles = meta.get("tiles", [])
        for tile_num_str, verdict in verdicts.items():
            tile_idx = int(tile_num_str) - 1
            if tile_idx < 0 or tile_idx >= len(tiles):
                continue

            tile_meta = tiles[tile_idx]
            results.append(
                {
                    "batch_id": batch_id,
                    "batch_type": batch_type,
                    "tile_num": int(tile_num_str),
                    "verdict": verdict,
                    "tile_id": tile_meta.get("id"),
                    "tile_path": tile_meta.get("tile_path"),
                }
            )

    return results


def apply_verdicts_to_db(
    conn: sqlite3.Connection,
    verdicts: list[dict],
) -> dict:
    """Write verdicts to the labels table in the SQLite cache.

    Returns stats dict.
    """
    stats = {"updated": 0, "tp": 0, "fp": 0, "uncertain": 0, "no_match": 0}

    for v in verdicts:
        verdict = v["verdict"]
        tile_id = v.get("tile_id")

        if tile_id is not None:
            conn.execute(
                "UPDATE labels SET qa_verdict = ?, qa_batch_id = ? WHERE id = ?",
                (verdict, v["batch_id"], tile_id),
            )
            stats["updated"] += 1
        else:
            stats["no_match"] += 1
            continue

        if verdict in TP_VERDICTS:
            stats["tp"] += 1
        elif verdict in FP_VERDICTS:
            stats["fp"] += 1
        else:
            stats["uncertain"] += 1

    conn.commit()
    return stats


def export_verdicts_by_label(
    conn: sqlite3.Connection,
    output_path: Path,
) -> int:
    """Export a machine-readable mapping of label paths to verdicts.

    Writes {label_path: verdict} JSON for labels that have been reviewed.
    """
    rows = conn.execute(
        "SELECT label_path, qa_verdict FROM labels WHERE qa_verdict IS NOT NULL"
    ).fetchall()

    mapping = {row[0]: row[1] for row in rows}
    output_path.write_text(json.dumps(mapping, indent=2))
    return len(mapping)


def ingest_game(
    conn: sqlite3.Connection,
    qa_dir: Path,
    game_id: str,
) -> dict:
    """Ingest all QA verdicts for one game."""
    game_dir = qa_dir / game_id
    all_stats = {"positive": {}, "negative": {}}

    pos_dir = game_dir / "positive_batches"
    if pos_dir.exists():
        pos_results = ingest_batch_results(pos_dir, "positive")
        if pos_results:
            all_stats["positive"] = apply_verdicts_to_db(conn, pos_results)
            logger.info(
                "%s positive: %d updated (%d TP, %d FP)",
                game_id,
                all_stats["positive"]["updated"],
                all_stats["positive"]["tp"],
                all_stats["positive"]["fp"],
            )

    neg_dir = game_dir / "negative_batches"
    if neg_dir.exists():
        neg_results = ingest_batch_results(neg_dir, "negative")
        if neg_results:
            all_stats["negative"] = apply_verdicts_to_db(conn, neg_results)
            logger.info(
                "%s negative: %d updated",
                game_id,
                all_stats["negative"]["updated"],
            )

    return all_stats


def main():
    parser = argparse.ArgumentParser(
        description="Ingest QA verdicts into label database"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    parser.add_argument(
        "--qa-dir",
        type=Path,
        default=DEFAULT_DB_PATH.parent,
        help="QA output directory (default: %(default)s)",
    )
    parser.add_argument("--games", nargs="+", help="Only ingest specific games")
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="Export verdicts-by-label JSON to this path",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = sqlite3.connect(str(args.db))

    if args.games:
        game_ids = args.games
    else:
        game_ids = [
            r[0]
            for r in conn.execute(
                "SELECT game_id FROM game_meta ORDER BY game_id"
            ).fetchall()
        ]

    for game_id in game_ids:
        ingest_game(conn, args.qa_dir, game_id)

    if args.export:
        n = export_verdicts_by_label(conn, args.export)
        logger.info("Exported %d verdicts to %s", n, args.export)

    conn.close()


if __name__ == "__main__":
    main()
