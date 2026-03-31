"""Exp 3: Frame differencing with person mask subtraction.

Subtract player regions before computing diff. Remaining motion =
non-player objects (hopefully the ball).

Uses person detection on r0 region to build masks.
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

OUTPUT = Path("D:/training_data/experiments/exp3_framediff_masked.json")
FINDINGS = Path("D:/training_data/experiments/exp3_findings.txt")

# Far field region in panoramic coordinates
FAR_Y_MIN = 0
FAR_Y_MAX = 700

# Frame diff parameters
DIFF_THRESHOLD = 50
MIN_BLOB_AREA = 15
MAX_BLOB_AREA = 400
MIN_CIRCULARITY = 0.4
FRAME_INTERVAL = 4

# Trajectory linking
MAX_LINK_DIST = 80  # pixels between consecutive frames
MIN_TRAJ_LENGTH = 3
MIN_PATH_LENGTH = 20  # pixels total displacement to count as "moving"

# Person masking
PERSON_EXPAND = 30  # expand person bbox by this many pixels


def detect_persons_in_frame(frame, person_model):
    """Run YOLO person detection on the far-field crop."""
    results = person_model(frame, conf=0.3, classes=[0], verbose=False)
    boxes = []
    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            boxes.append((int(x1), int(y1), int(x2), int(y2)))
    return boxes


def create_person_mask(shape, person_boxes, expand=PERSON_EXPAND):
    """Create binary mask where persons are 255 (to be masked out)."""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    for x1, y1, x2, y2 in person_boxes:
        cv2.rectangle(
            mask,
            (max(0, x1 - expand), max(0, y1 - expand)),
            (min(shape[1], x2 + expand), min(shape[0], y2 + expand)),
            255,
            -1,
        )
    return mask


def process_segment(video_path: Path, game_id: str, max_frames: int = 2000):
    """Run person-masked frame diff on one video segment."""
    from ultralytics import YOLO

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return []

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    seg_name = video_path.stem
    logger.info("  %s: %d frames (processing %d)", seg_name[:40], total, min(total, max_frames))

    # Load person detector (CPU)
    person_model = YOLO("yolo11n.pt")

    prev_gray = None
    candidates = []  # (frame_idx, x, y, area, circularity)
    person_counts = []

    for fi in range(min(total, max_frames)):
        ret = cap.grab()
        if not ret:
            break
        if fi % FRAME_INTERVAL != 0:
            continue

        ret, frame = cap.retrieve()
        if not ret:
            continue

        # Crop to far field
        far_crop = frame[FAR_Y_MIN:FAR_Y_MAX, :]
        gray = cv2.cvtColor(far_crop, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None:
            # Detect persons in current far-field crop
            person_boxes = detect_persons_in_frame(far_crop, person_model)
            person_counts.append(len(person_boxes))

            # Create person mask
            person_mask = create_person_mask(gray.shape, person_boxes)

            # Frame diff
            diff = cv2.absdiff(prev_gray, gray)
            diff = cv2.GaussianBlur(diff, (5, 5), 0)
            _, binary = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

            # Morphological cleanup
            kernel = np.ones((3, 3), np.uint8)
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

            # Mask out person regions
            binary = cv2.bitwise_and(binary, cv2.bitwise_not(person_mask))

            # Find contours
            contours, _ = cv2.findContours(
                binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for c in contours:
                area = cv2.contourArea(c)
                if area < MIN_BLOB_AREA or area > MAX_BLOB_AREA:
                    continue
                perimeter = cv2.arcLength(c, True)
                if perimeter == 0:
                    continue
                circ = 4 * np.pi * area / (perimeter**2)
                if circ < MIN_CIRCULARITY:
                    continue

                M = cv2.moments(c)
                if M["m00"] == 0:
                    continue
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"]) + FAR_Y_MIN  # back to panoramic coords

                candidates.append((fi, cx, cy, area, circ))

        prev_gray = gray

    cap.release()

    # Link into trajectories
    frame_groups = defaultdict(list)
    for fi, x, y, area, circ in candidates:
        frame_groups[fi].append((x, y, area, circ))

    trajectories = []
    active = []
    for fi in sorted(frame_groups.keys()):
        blobs = frame_groups[fi]
        used = [False] * len(blobs)
        new_active = []

        for traj in active:
            last_fi, last_x, last_y = traj[-1][:3]
            if fi - last_fi > FRAME_INTERVAL * 3:
                if len(traj) >= MIN_TRAJ_LENGTH:
                    trajectories.append(traj)
                continue

            best_idx = -1
            best_dist = MAX_LINK_DIST
            for j, (bx, by, ba, bc) in enumerate(blobs):
                if used[j]:
                    continue
                dist = ((bx - last_x) ** 2 + (by - last_y) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_idx = j

            if best_idx >= 0:
                bx, by, ba, bc = blobs[best_idx]
                traj.append((fi, bx, by, ba, bc))
                used[best_idx] = True
                new_active.append(traj)
            else:
                if len(traj) >= MIN_TRAJ_LENGTH:
                    trajectories.append(traj)

        for j, (bx, by, ba, bc) in enumerate(blobs):
            if not used[j]:
                new_active.append([(fi, bx, by, ba, bc)])
        active = new_active

    for traj in active:
        if len(traj) >= MIN_TRAJ_LENGTH:
            trajectories.append(traj)

    # Classify: moving vs static
    results = []
    for traj in trajectories:
        path_len = sum(
            ((traj[i][1] - traj[i - 1][1]) ** 2 + (traj[i][2] - traj[i - 1][2]) ** 2) ** 0.5
            for i in range(1, len(traj))
        )
        if path_len < MIN_PATH_LENGTH:
            continue  # static noise

        results.append({
            "game_id": game_id,
            "segment": seg_name,
            "frames": [(t[0], t[1], t[2]) for t in traj],
            "path_length": round(path_len, 1),
            "avg_area": round(sum(t[3] for t in traj) / len(traj), 1),
            "avg_circularity": round(sum(t[4] for t in traj) / len(traj), 2),
            "source": "framediff_person_masked",
        })

    avg_persons = sum(person_counts) / len(person_counts) if person_counts else 0
    logger.info(
        "    %d candidates -> %d trajectories -> %d moving (avg %.1f persons/frame)",
        len(candidates), len(trajectories), len(results), avg_persons,
    )
    return results


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    # Use segments with most ONNX gaps (from Exp 1)
    test_segments = {
        "heat__05.31.2024_vs_Fairport_home": [
            "D:/training_data/videos/heat__05.31.2024_vs_Fairport_home",
        ],
        "flash__06.01.2024_vs_IYSA_home": [
            "D:/training_data/videos/flash__06.01.2024_vs_IYSA_home",
        ],
    }

    start = time.time()
    all_results = []
    findings = []

    for game_id, video_dirs in test_segments.items():
        logger.info("=== %s ===", game_id)
        for vdir in video_dirs:
            vdir_path = Path(vdir)
            if not vdir_path.exists():
                logger.warning("Video dir not found: %s", vdir)
                continue
            for seg in sorted(vdir_path.glob("*.mp4")):
                results = process_segment(seg, game_id, max_frames=2000)
                all_results.extend(results)

    elapsed = time.time() - start

    with open(OUTPUT, "w") as f:
        json.dump(all_results, f, indent=2)

    # Record findings
    findings.append(f"Exp 3: Frame Diff with Person Masking")
    findings.append(f"Time: {elapsed:.0f}s")
    findings.append(f"Total moving trajectories: {len(all_results)}")
    findings.append(f"Target: < 500 per 200 frames. Got: {len(all_results)} total across all segments")
    if all_results:
        avg_path = sum(r["path_length"] for r in all_results) / len(all_results)
        avg_area = sum(r["avg_area"] for r in all_results) / len(all_results)
        avg_circ = sum(r["avg_circularity"] for r in all_results) / len(all_results)
        avg_len = sum(len(r["frames"]) for r in all_results) / len(all_results)
        findings.append(f"Avg path length: {avg_path:.0f}px")
        findings.append(f"Avg blob area: {avg_area:.0f}px²")
        findings.append(f"Avg circularity: {avg_circ:.2f}")
        findings.append(f"Avg trajectory length: {avg_len:.1f} frames")

    with open(FINDINGS, "w") as f:
        f.write("\n".join(findings))

    logger.info("\n".join(findings))
    logger.info("Results: %s", OUTPUT)


if __name__ == "__main__":
    main()
