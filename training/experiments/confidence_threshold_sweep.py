"""Confidence threshold sweep experiment.

Compares external detector detections at various confidence thresholds
against user-marked ground truth to find the optimal threshold.

Usage:
    python -m training.experiments.confidence_threshold_sweep \
        --detections F:/training_data/ext_detections_chili_seg5_fp16_lowconf.json \
        --feedback review_packets/tracking_lab/feedback.json
"""

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


def load_ground_truth(feedback_path: Path) -> dict[int, tuple[float, float]]:
    """Load user marks as ground truth: {frame_idx: (pano_x, pano_y)}."""
    with open(feedback_path) as f:
        fb = json.load(f)

    marks = {}
    for entry in fb:
        if entry.get("action") == "mark_ball" and "row" in entry and "col" in entry:
            fi = entry["frame_idx"]
            px = entry["col"] * 576 + entry["x"]
            py = entry["row"] * 580 + entry["y"]
            marks[fi] = (px, py)

    return marks


def sweep_thresholds(
    detections: list[dict],
    ground_truth: dict[int, tuple[float, float]],
    thresholds: list[float],
    match_radius: float = 100.0,
) -> list[dict]:
    """Run threshold sweep and compute metrics.

    For each threshold:
    - Filter detections to conf >= threshold
    - Apply field boundary filter
    - For each ground truth frame, find nearest detection
    - Compute recall, precision proxy, avg distance

    Returns list of {threshold, recall_100, recall_200, avg_dist, ...}
    """
    # Inline field check to avoid cv2 dependency
    def is_on_field_curved(x, y, margin=50.0):
        pano_cx = 2048.0
        y_top = 310.0 + 0.0000285 * (x - pano_cx) ** 2 - margin
        y_bot = 1600.0 - 0.0000220 * (x - pano_cx) ** 2 + margin
        return y_top <= y <= y_bot

    # Group detections by frame
    by_frame: dict[int, list[dict]] = defaultdict(list)
    for d in detections:
        by_frame[d["frame_idx"]].append(d)

    gt_frames = sorted(ground_truth.keys())
    results = []

    for thresh in thresholds:
        # Filter by confidence + field boundary
        filtered_by_frame: dict[int, list[dict]] = {}
        total_dets = 0
        for fi, dets in by_frame.items():
            good = [
                d for d in dets
                if d["conf"] >= thresh and is_on_field_curved(d["cx"], d["cy"], margin=50)
            ]
            if good:
                filtered_by_frame[fi] = good
                total_dets += len(good)

        # Static removal
        pos_counts: dict[tuple, int] = defaultdict(int)
        for fi, dets in filtered_by_frame.items():
            for d in dets:
                key = (round(d["cx"] / 50) * 50, round(d["cy"] / 50) * 50)
                pos_counts[key] += 1
        static = {k for k, v in pos_counts.items() if v > 100}

        clean_by_frame: dict[int, list[dict]] = {}
        clean_total = 0
        for fi, dets in filtered_by_frame.items():
            clean = [
                d for d in dets
                if (round(d["cx"] / 50) * 50, round(d["cy"] / 50) * 50) not in static
            ]
            if clean:
                clean_by_frame[fi] = clean
                clean_total += len(clean)

        # Evaluate against ground truth
        match_100 = 0
        match_200 = 0
        match_300 = 0
        no_det = 0
        distances = []
        false_pos_per_frame = []

        for fi in gt_frames:
            ux, uy = ground_truth[fi]
            dets = clean_by_frame.get(fi, [])

            if not dets:
                no_det += 1
                continue

            # Find closest detection to ground truth
            closest = min(dets, key=lambda d: (d["cx"] - ux) ** 2 + (d["cy"] - uy) ** 2)
            dist = ((closest["cx"] - ux) ** 2 + (closest["cy"] - uy) ** 2) ** 0.5
            distances.append(dist)

            if dist < 100:
                match_100 += 1
            if dist < 200:
                match_200 += 1
            if dist < 300:
                match_300 += 1

            # Count false positives (detections far from mark)
            fp = sum(1 for d in dets if ((d["cx"] - ux) ** 2 + (d["cy"] - uy) ** 2) ** 0.5 > 300)
            false_pos_per_frame.append(fp)

        n = len(gt_frames)
        avg_dist = sum(distances) / len(distances) if distances else 0
        avg_fp = sum(false_pos_per_frame) / len(false_pos_per_frame) if false_pos_per_frame else 0

        # Per-region breakdown
        region_stats = {}
        for region_name, row_filter in [
            ("r0_far", lambda m: m["row"] == 0),
            ("r1_mid", lambda m: m["row"] == 1),
            ("r2_near", lambda m: m["row"] == 2),
        ]:
            region_frames = [
                fi for fi in gt_frames
                if any(
                    entry.get("action") == "mark_ball"
                    and entry["frame_idx"] == fi
                    and row_filter(entry)
                    for entry in []  # placeholder — need raw feedback
                )
            ]
            # Simplified: use ground truth y-coordinate to determine region
            r_frames = []
            for fi in gt_frames:
                _, uy = ground_truth[fi]
                if region_name == "r0_far" and uy < 580:
                    r_frames.append(fi)
                elif region_name == "r1_mid" and 580 <= uy < 1160:
                    r_frames.append(fi)
                elif region_name == "r2_near" and uy >= 1160:
                    r_frames.append(fi)

            r_match = 0
            for fi in r_frames:
                ux, uy = ground_truth[fi]
                dets = clean_by_frame.get(fi, [])
                if dets:
                    closest = min(dets, key=lambda d: (d["cx"] - ux) ** 2 + (d["cy"] - uy) ** 2)
                    if ((closest["cx"] - ux) ** 2 + (closest["cy"] - uy) ** 2) ** 0.5 < 200:
                        r_match += 1

            region_stats[region_name] = {
                "total": len(r_frames),
                "matched_200": r_match,
                "recall_200": r_match / len(r_frames) if r_frames else 0,
            }

        result = {
            "threshold": thresh,
            "total_dets": clean_total,
            "dets_per_frame": clean_total / len(by_frame) if by_frame else 0,
            "recall_100": match_100 / n,
            "recall_200": match_200 / n,
            "recall_300": match_300 / n,
            "no_detection": no_det / n,
            "avg_distance": round(avg_dist),
            "avg_false_pos": round(avg_fp, 1),
            "static_removed": len(static),
            "regions": region_stats,
        }
        results.append(result)

    return results


def print_results(results: list[dict]):
    """Print results as a formatted table."""
    print(f"\n{'Thresh':>7s} {'Dets':>6s} {'D/F':>5s} {'R@100':>6s} {'R@200':>6s} {'R@300':>6s} {'NoDet':>6s} {'AvgDist':>7s} {'FP/F':>5s} {'Static':>6s}")
    print("-" * 75)
    for r in results:
        print(
            f"{r['threshold']:7.2f} "
            f"{r['total_dets']:6d} "
            f"{r['dets_per_frame']:5.1f} "
            f"{r['recall_100']:6.1%} "
            f"{r['recall_200']:6.1%} "
            f"{r['recall_300']:6.1%} "
            f"{r['no_detection']:6.1%} "
            f"{r['avg_distance']:7d} "
            f"{r['avg_false_pos']:5.1f} "
            f"{r['static_removed']:6d}"
        )

    # Per-region breakdown
    print(f"\n{'Region breakdown (Recall@200):':}")
    print(f"{'Thresh':>7s}", end="")
    for region in ["r0_far", "r1_mid", "r2_near"]:
        print(f" {region:>10s}", end="")
    print()
    for r in results:
        print(f"{r['threshold']:7.2f}", end="")
        for region in ["r0_far", "r1_mid", "r2_near"]:
            rs = r["regions"].get(region, {})
            total = rs.get("total", 0)
            recall = rs.get("recall_200", 0)
            print(f" {recall:8.1%}({total:d})", end="")
        print()

    # Recommendation
    print("\n--- Recommendation ---")
    best_recall = max(results, key=lambda r: r["recall_200"])
    best_balance = max(results, key=lambda r: r["recall_200"] - r["avg_false_pos"] * 0.1)
    print(f"Best recall@200:  conf={best_recall['threshold']:.2f} ({best_recall['recall_200']:.1%})")
    print(f"Best balance:     conf={best_balance['threshold']:.2f} (recall={best_balance['recall_200']:.1%}, FP={best_balance['avg_false_pos']:.1f}/frame)")


def main():
    parser = argparse.ArgumentParser(description="Confidence threshold sweep")
    parser.add_argument("--detections", type=Path, required=True)
    parser.add_argument("--feedback", type=Path, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with open(args.detections) as f:
        detections = json.load(f)
    logger.info("Loaded %d detections", len(detections))

    gt = load_ground_truth(args.feedback)
    logger.info("Loaded %d ground truth marks", len(gt))

    thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80]
    results = sweep_thresholds(detections, gt, thresholds)
    print_results(results)

    # Save results
    out = args.detections.parent / "threshold_sweep_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Saved results to %s", out)


if __name__ == "__main__":
    main()
