"""Aggregate Sonnet agent QA results into a categorized report.

Reads *_result.json files from positive/negative batch directories,
joins with the SQLite cache for metadata, and generates report.json + report.txt.

Usage:
    uv run python -m training.label_qa_report
    uv run python -m training.label_qa_report --games flash__09.27.2024_vs_RNYFC_Black_home
"""

import argparse
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from training.label_qa_cache import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def load_batch_results(batch_dir: Path) -> list[dict]:
    """Load all *_result.json files from a batch directory.

    Returns list of {batch_id, tile_num, verdict} dicts.
    """
    results = []
    for result_file in sorted(batch_dir.glob("*_result.json")):
        batch_id = result_file.stem.replace("_result", "")
        try:
            verdicts = json.loads(result_file.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Bad result file %s: %s", result_file, e)
            continue

        # Load matching metadata
        meta_file = batch_dir / f"{batch_id}.json"
        meta = None
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except (json.JSONDecodeError, ValueError):
                pass

        for tile_num_str, verdict in verdicts.items():
            tile_idx = int(tile_num_str) - 1  # 1-indexed to 0-indexed
            tile_meta = None
            if meta and "tiles" in meta and tile_idx < len(meta["tiles"]):
                tile_meta = meta["tiles"][tile_idx]

            results.append(
                {
                    "batch_id": batch_id,
                    "tile_num": int(tile_num_str),
                    "verdict": verdict,
                    "meta": tile_meta,
                }
            )

    return results


def compute_game_stats(
    conn: sqlite3.Connection,
    game_id: str,
    qa_dir: Path,
) -> dict:
    """Compute QA statistics for a single game."""
    game_dir = qa_dir / game_id
    stats = {
        "game_id": game_id,
        "positive_audit": {},
        "negative_audit": {},
        "field_mask": {},
        "summary": {},
    }

    # Field mask stats from DB
    meta_row = conn.execute(
        "SELECT total_labels, total_positives, positives_in_field FROM game_meta WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if meta_row:
        total, positives, in_field = meta_row
        stats["field_mask"] = {
            "total_labels": total,
            "total_positives": positives,
            "positives_in_field": in_field or 0,
            "filtered_out": positives - (in_field or 0),
            "filter_rate": round(1 - (in_field or 0) / positives, 3)
            if positives
            else 0,
        }

    # Positive audit results
    pos_dir = game_dir / "positive_batches"
    if pos_dir.exists():
        pos_results = load_batch_results(pos_dir)
        if pos_results:
            verdict_counts = Counter(r["verdict"] for r in pos_results)
            total_reviewed = len(pos_results)
            tp = verdict_counts.get("TRUE_POSITIVE", 0)
            fp_total = sum(
                v
                for k, v in verdict_counts.items()
                if k.startswith("FP_") or k == "FP_NOT_BALL"
            )

            # FP breakdown by type
            fp_breakdown = {
                k: v for k, v in verdict_counts.items() if k.startswith("FP_")
            }

            # FP by tile position
            fp_by_position = defaultdict(int)
            tp_by_position = defaultdict(int)
            for r in pos_results:
                if r["meta"] and "row" in r["meta"] and "col" in r["meta"]:
                    pos_key = f"r{r['meta']['row']}_c{r['meta']['col']}"
                    if r["verdict"] == "TRUE_POSITIVE":
                        tp_by_position[pos_key] += 1
                    elif r["verdict"].startswith("FP_"):
                        fp_by_position[pos_key] += 1

            stats["positive_audit"] = {
                "total_reviewed": total_reviewed,
                "verdict_counts": dict(verdict_counts),
                "true_positive_count": tp,
                "false_positive_count": fp_total,
                "true_positive_rate": round(tp / total_reviewed, 3)
                if total_reviewed
                else 0,
                "false_positive_rate": round(fp_total / total_reviewed, 3)
                if total_reviewed
                else 0,
                "fp_breakdown": fp_breakdown,
                "fp_by_position": dict(fp_by_position),
                "tp_by_position": dict(tp_by_position),
            }

    # Negative audit results
    neg_dir = game_dir / "negative_batches"
    if neg_dir.exists():
        neg_results = load_batch_results(neg_dir)
        if neg_results:
            verdict_counts = Counter(r["verdict"] for r in neg_results)
            total_reviewed = len(neg_results)
            tn = verdict_counts.get("TRUE_NEGATIVE", 0)
            fn = verdict_counts.get("FALSE_NEGATIVE", 0)

            stats["negative_audit"] = {
                "total_reviewed": total_reviewed,
                "verdict_counts": dict(verdict_counts),
                "true_negative_count": tn,
                "false_negative_count": fn,
                "false_negative_rate": round(fn / total_reviewed, 3)
                if total_reviewed
                else 0,
            }

    # Summary
    pos = stats.get("positive_audit", {})
    neg = stats.get("negative_audit", {})
    stats["summary"] = {
        "positive_audited": pos.get("total_reviewed", 0),
        "tp_rate": pos.get("true_positive_rate", None),
        "fp_rate": pos.get("false_positive_rate", None),
        "negative_audited": neg.get("total_reviewed", 0),
        "fn_rate": neg.get("false_negative_rate", None),
    }

    return stats


def generate_report(
    conn: sqlite3.Connection,
    qa_dir: Path,
    game_ids: list[str],
) -> dict:
    """Generate the full aggregate report."""
    per_game = {}
    for game_id in game_ids:
        game_stats = compute_game_stats(conn, game_id, qa_dir)
        per_game[game_id] = game_stats

    # Aggregate across games
    total_pos_reviewed = 0
    total_tp = 0
    total_fp = 0
    total_neg_reviewed = 0
    total_fn = 0
    aggregate_fp_breakdown = Counter()
    aggregate_fp_by_position = Counter()

    for gs in per_game.values():
        pa = gs.get("positive_audit", {})
        total_pos_reviewed += pa.get("total_reviewed", 0)
        total_tp += pa.get("true_positive_count", 0)
        total_fp += pa.get("false_positive_count", 0)
        aggregate_fp_breakdown.update(pa.get("fp_breakdown", {}))
        aggregate_fp_by_position.update(pa.get("fp_by_position", {}))

        na = gs.get("negative_audit", {})
        total_neg_reviewed += na.get("total_reviewed", 0)
        total_fn += na.get("false_negative_count", 0)

    # Field mask aggregate
    total_labels = sum(
        gs.get("field_mask", {}).get("total_positives", 0) for gs in per_game.values()
    )
    total_in_field = sum(
        gs.get("field_mask", {}).get("positives_in_field", 0)
        for gs in per_game.values()
    )

    report = {
        "summary": {
            "games_audited": len(per_game),
            "total_ext_detections": total_labels,
            "in_field_detections": total_in_field,
            "field_mask_filter_rate": round(1 - total_in_field / total_labels, 3)
            if total_labels
            else 0,
            "positive_audited": total_pos_reviewed,
            "true_positive_rate": round(total_tp / total_pos_reviewed, 3)
            if total_pos_reviewed
            else None,
            "false_positive_rate": round(total_fp / total_pos_reviewed, 3)
            if total_pos_reviewed
            else None,
            "fp_breakdown": dict(aggregate_fp_breakdown),
            "fp_by_tile_position": dict(aggregate_fp_by_position),
            "negative_audited": total_neg_reviewed,
            "false_negative_rate": round(total_fn / total_neg_reviewed, 3)
            if total_neg_reviewed
            else None,
        },
        "per_game": per_game,
        "recommendations": [],
    }

    # Generate recommendations
    recs = report["recommendations"]
    if total_pos_reviewed > 0:
        fp_rate = total_fp / total_pos_reviewed
        if fp_rate > 0.3:
            recs.append(
                f"High FP rate ({fp_rate:.0%}): consider raising detection confidence threshold"
            )
        if aggregate_fp_breakdown.get("FP_NOT_BALL", 0) > total_fp * 0.3:
            recs.append(
                "Many FP_NOT_BALL: model confusing non-ball objects. "
                "May need more negative training examples or better augmentation"
            )
        if aggregate_fp_breakdown.get("FP_NOT_GAME_BALL", 0) > total_fp * 0.2:
            recs.append(
                "Significant FP_NOT_GAME_BALL: warmup/sideline balls detected. "
                "Consider game phase filtering or single-ball constraint"
            )
        if aggregate_fp_breakdown.get("FP_OFF_FIELD", 0) > total_fp * 0.1:
            recs.append(
                "FP_OFF_FIELD detections remain after field mask: "
                "consider tightening field mask margin"
            )

    if total_neg_reviewed > 0:
        fn_rate = total_fn / total_neg_reviewed
        if fn_rate > 0.1:
            recs.append(
                f"High FN rate ({fn_rate:.0%}): model missing balls. "
                "Consider lower confidence threshold or more training data"
            )

    return report


def format_text_report(report: dict) -> str:
    """Format report as human-readable text."""
    lines = []
    s = report["summary"]

    lines.append("=" * 60)
    lines.append("BALL LABEL QA REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Games audited: {s['games_audited']}")
    lines.append(f"Total ext detections: {s['total_ext_detections']:,}")
    lines.append(f"In-field after mask: {s['in_field_detections']:,}")
    lines.append(f"Field mask filter rate: {s['field_mask_filter_rate']:.1%}")
    lines.append("")

    lines.append("--- POSITIVE AUDIT (False Positive Detection) ---")
    lines.append(f"Tiles reviewed: {s['positive_audited']}")
    if s["true_positive_rate"] is not None:
        lines.append(f"True positive rate: {s['true_positive_rate']:.1%}")
        lines.append(f"False positive rate: {s['false_positive_rate']:.1%}")
    lines.append("")

    if s.get("fp_breakdown"):
        lines.append("FP breakdown:")
        for k, v in sorted(s["fp_breakdown"].items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v}")
        lines.append("")

    if s.get("fp_by_tile_position"):
        lines.append("FP by tile position:")
        for k, v in sorted(s["fp_by_tile_position"].items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v}")
        lines.append("")

    lines.append("--- NEGATIVE AUDIT (False Negative Detection) ---")
    lines.append(f"Tiles reviewed: {s['negative_audited']}")
    if s["false_negative_rate"] is not None:
        lines.append(f"False negative rate: {s['false_negative_rate']:.1%}")
    lines.append("")

    if report.get("recommendations"):
        lines.append("--- RECOMMENDATIONS ---")
        for i, rec in enumerate(report["recommendations"], 1):
            lines.append(f"  {i}. {rec}")
        lines.append("")

    lines.append("--- PER-GAME BREAKDOWN ---")
    for game_id, gs in report["per_game"].items():
        fm = gs.get("field_mask", {})
        pa = gs.get("positive_audit", {})
        na = gs.get("negative_audit", {})
        lines.append(f"\n  {game_id}:")
        if fm:
            lines.append(
                f"    Detections: {fm.get('total_positives', 0):,} total, "
                f"{fm.get('positives_in_field', 0):,} in-field "
                f"({fm.get('filter_rate', 0):.1%} filtered)"
            )
        if pa:
            lines.append(
                f"    Positive audit: {pa.get('total_reviewed', 0)} reviewed, "
                f"TP={pa.get('true_positive_rate', 0):.0%}, "
                f"FP={pa.get('false_positive_rate', 0):.0%}"
            )
        if na:
            lines.append(
                f"    Negative audit: {na.get('total_reviewed', 0)} reviewed, "
                f"FN={na.get('false_negative_rate', 0):.0%}"
            )

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate QA report from agent results"
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
        default=DEFAULT_DB_PATH.parent,
        help="Output directory (default: %(default)s)",
    )
    parser.add_argument("--games", nargs="+", help="Only report specific games")
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

    report = generate_report(conn, args.output, game_ids)

    # Write JSON report
    json_path = args.output / "report.json"
    json_path.write_text(json.dumps(report, indent=2))
    logger.info("JSON report: %s", json_path)

    # Write text report
    txt_path = args.output / "report.txt"
    text = format_text_report(report)
    txt_path.write_text(text)
    logger.info("Text report: %s", txt_path)
    print(text)

    conn.close()


if __name__ == "__main__":
    main()
