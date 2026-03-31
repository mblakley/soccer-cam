"""Exp 3b Full Scale: Verify ONNX gap positions with targeted frame diff.

For each gap from Exp 1, seek to that frame, check for motion blob
near the predicted position. Record matches with color/shape analysis.
"""
import cv2
import json
import logging
import numpy as np
import time
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

GAPS_FILE = Path("D:/training_data/experiments/exp1_onnx_gaps.json")
OUTPUT = Path("D:/training_data/experiments/exp3b_fullscale_results.json")
FINDINGS = Path("D:/training_data/experiments/exp3b_fullscale_findings.txt")

# Video locations on D: (fast) and F: (slow fallback)
VIDEO_DIRS = {
    "heat__05.31.2024_vs_Fairport_home": [
        Path("D:/training_data/videos/heat__05.31.2024_vs_Fairport_home"),
        Path("F:/Heat_2012s/05.31.2024 - vs Fairport (home)"),
    ],
    "flash__06.01.2024_vs_IYSA_home": [
        Path("D:/training_data/videos/flash__06.01.2024_vs_IYSA_home"),
        Path("F:/Flash_2013s/06.01.2024 - vs IYSA (home)"),
    ],
    # Add more as videos are copied to D:
    "flash__09.27.2024_vs_RNYFC_Black_home": [
        Path("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)"),
    ],
    "flash__09.30.2024_vs_Chili_home": [
        Path("F:/Flash_2013s/09.30.2024 - vs Chili (home)"),
    ],
    "flash__2025.06.02": [
        Path("F:/Flash_2013s/2025.06.02-18.16.03"),
    ],
    "heat__06.20.2024_vs_Chili_away": [
        Path("F:/Heat_2012s/06.20.2024 - vs Chili (away)"),
    ],
    "heat__07.17.2024_vs_Fairport_away": [
        Path("F:/Heat_2012s/07.17.2024 - vs Fairport (away)"),
    ],
    "heat__Clarence_Tournament": [
        Path("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament"),
    ],
    "heat__Heat_Tournament": [
        Path("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament"),
    ],
}

SEARCH_RADIUS = 80
DIFF_THRESHOLD = 35
MIN_AREA = 8
MAX_AREA = 500


def find_video(game_id: str, segment: str) -> Path | None:
    """Find the video file for a segment."""
    dirs = VIDEO_DIRS.get(game_id, [])
    for vdir in dirs:
        if not vdir.exists():
            continue
        for f in vdir.iterdir():
            if f.suffix == ".mp4" and segment[:15] in f.name:
                return f
    return None


def verify_gaps_in_segment(
    video_path: Path, segment_gaps: list[dict]
) -> list[dict]:
    """Verify gap positions with frame diff for one segment."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    results = []

    for g in segment_gaps:
        fi = g["frame_idx"]
        pred_x = g["pano_x"]
        pred_y = g["pano_y"]

        if fi < 4 or fi >= total:
            continue

        # Read prev and current frames
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi - 4)
        ret, prev_frame = cap.read()
        if not ret:
            continue

        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ret, curr_frame = cap.read()
        if not ret:
            continue

        # Far field crop + diff
        prev_gray = cv2.cvtColor(prev_frame[:700, :], cv2.COLOR_BGR2GRAY)
        curr_gray = cv2.cvtColor(curr_frame[:700, :], cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(prev_gray, curr_gray)
        diff = cv2.GaussianBlur(diff, (5, 5), 0)

        pred_y_int = min(int(pred_y), 699)
        pred_x_int = int(pred_x)
        x1 = max(0, pred_x_int - SEARCH_RADIUS)
        x2 = min(diff.shape[1], pred_x_int + SEARCH_RADIUS)
        y1 = max(0, pred_y_int - SEARCH_RADIUS)
        y2 = min(diff.shape[0], pred_y_int + SEARCH_RADIUS)

        window = diff[y1:y2, x1:x2]
        _, binary = cv2.threshold(window, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        best_blob = None
        best_dist = SEARCH_RADIUS

        for c in contours:
            area = cv2.contourArea(c)
            if area < MIN_AREA or area > MAX_AREA:
                continue
            perimeter = cv2.arcLength(c, True)
            if perimeter == 0:
                continue
            circ = 4 * np.pi * area / (perimeter**2)

            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"]) + x1
            cy = int(M["m01"] / M["m00"]) + y1

            dist = ((cx - pred_x_int) ** 2 + (cy - pred_y_int) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                patch = curr_frame[max(0, cy - 5) : cy + 5, max(0, cx - 5) : cx + 5]
                avg_brightness = float(patch.mean()) if patch.size > 0 else 0
                best_blob = {
                    "game_id": g["game_id"],
                    "segment": g["segment"],
                    "frame_idx": fi,
                    "pred_x": pred_x,
                    "pred_y": pred_y,
                    "blob_x": cx,
                    "blob_y": cy,
                    "distance": round(dist, 1),
                    "area": area,
                    "circularity": round(circ, 2),
                    "brightness": round(avg_brightness, 1),
                    "traj_len": g["trajectory_length"],
                    "source": "exp3b_targeted_framediff",
                }

        if best_blob:
            results.append(best_blob)

    cap.release()
    return results


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    gaps = json.load(open(GAPS_FILE))
    logger.info("Loaded %d gap candidates", len(gaps))

    # Group by game + segment
    by_seg = defaultdict(list)
    for g in gaps:
        by_seg[(g["game_id"], g["segment"])].append(g)

    start = time.time()
    all_results = []
    game_stats = defaultdict(lambda: {"checked": 0, "found": 0, "high_conf": 0})

    for (game_id, segment), seg_gaps in sorted(by_seg.items()):
        video = find_video(game_id, segment)
        if video is None:
            continue

        results = verify_gaps_in_segment(video, seg_gaps)
        all_results.extend(results)

        stats = game_stats[game_id]
        stats["checked"] += len(seg_gaps)
        stats["found"] += len(results)
        high = [
            r
            for r in results
            if r["distance"] < 40 and r["brightness"] > 120 and r["circularity"] > 0.4
        ]
        stats["high_conf"] += len(high)

        logger.info(
            "  %s/%s: %d/%d verified (%d high-conf)",
            game_id[:20],
            segment[:25],
            len(results),
            len(seg_gaps),
            len(high),
        )

    elapsed = time.time() - start

    with open(OUTPUT, "w") as f:
        json.dump(all_results, f, indent=2)

    # Findings
    findings = [
        f"Exp 3b Full Scale Results",
        f"Time: {elapsed:.0f}s",
        f"Total verified: {len(all_results)} / {len(gaps)} gaps ({len(all_results)/max(len(gaps),1)*100:.0f}%)",
        "",
        "Per-game breakdown:",
    ]
    total_high = 0
    for game_id in sorted(game_stats):
        s = game_stats[game_id]
        findings.append(
            f"  {game_id}: {s['found']}/{s['checked']} verified, {s['high_conf']} high-conf"
        )
        total_high += s["high_conf"]

    findings.append(f"\nTotal high-confidence: {total_high}")
    findings.append(f"Target: >= 200 total. {'PASSED' if total_high >= 200 else 'NEED MORE'}")

    with open(FINDINGS, "w") as f:
        f.write("\n".join(findings))

    for line in findings:
        logger.info(line)


if __name__ == "__main__":
    main()
