"""Experiments for detecting small balls in the far field."""
import cv2
import numpy as np
import json
import time
from pathlib import Path

video_dir = Path("F:/Heat_2012s/05.31.2024 - vs Fairport (home)")
seg = next(f for f in video_dir.iterdir() if f.suffix == ".mp4" and "[F]" in f.name)
cap = cv2.VideoCapture(str(seg))
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
print(f"Video: {total} frames, {w}x{h}")

# EXPERIMENT 1: What size are known balls in the far field?
print("\n=== EXP 1: Known ball sizes in far field (from ONNX labels) ===", flush=True)
label_dir = Path("F:/training_data/labels_640_ext/heat__05.31.2024_vs_Fairport_home")
far_balls = []
for lf in list(label_dir.glob("*.txt"))[:5000]:
    if "_r0_" not in lf.name:
        continue
    for line in open(lf):
        p = line.strip().split()
        if len(p) >= 5:
            bw, bh = float(p[3]) * 640, float(p[4]) * 640
            far_balls.append((bw, bh))

if far_balls:
    widths = [b[0] for b in far_balls]
    heights = [b[1] for b in far_balls]
    print(f"  {len(far_balls)} far-field (r0) detections")
    print(f"  Width:  min={min(widths):.0f} max={max(widths):.0f} avg={sum(widths)/len(widths):.0f} px")
    print(f"  Height: min={min(heights):.0f} max={max(heights):.0f} avg={sum(heights)/len(heights):.0f} px")
else:
    print("  No far-field detections found")

# EXPERIMENT 2: Frame diff with shape filters at multiple thresholds
print("\n=== EXP 2: Frame diff threshold sweep ===", flush=True)
cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
prev = None
threshold_results = {t: {"round_small": 0, "total": 0} for t in [30, 40, 50, 60]}
N_FRAMES = 200

for i in range(N_FRAMES * 4):
    ret = cap.grab()
    if not ret:
        break
    if i % 4 == 0:
        ret, frame = cap.retrieve()
        if ret and prev is not None:
            gray = cv2.cvtColor(frame[:800, :], cv2.COLOR_BGR2GRAY)
            gray_prev = cv2.cvtColor(prev[:800, :], cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(gray_prev, gray)
            diff = cv2.GaussianBlur(diff, (5, 5), 0)

            for thresh in threshold_results:
                _, binary = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
                kernel = np.ones((3, 3), np.uint8)
                binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
                contours, _ = cv2.findContours(
                    binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                for c in contours:
                    area = cv2.contourArea(c)
                    threshold_results[thresh]["total"] += 1
                    if 15 < area < 300:
                        perimeter = cv2.arcLength(c, True)
                        if perimeter > 0:
                            circ = 4 * np.pi * area / (perimeter**2)
                            if circ > 0.5:
                                threshold_results[thresh]["round_small"] += 1
        if ret:
            prev = frame

for thresh, r in threshold_results.items():
    print(
        f"  thresh={thresh}: {r['round_small']} round_small ({r['round_small']/N_FRAMES:.1f}/frame), "
        f"{r['total']} total ({r['total']/N_FRAMES:.0f}/frame)"
    )

# EXPERIMENT 3: Trajectory linking at best threshold
print("\n=== EXP 3: Trajectory linking (thresh=50) ===", flush=True)
cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
prev = None
candidates = []  # (frame_idx, x, y, area)

for i in range(N_FRAMES * 4):
    ret = cap.grab()
    if not ret:
        break
    if i % 4 == 0:
        ret, frame = cap.retrieve()
        if ret and prev is not None:
            gray = cv2.cvtColor(frame[:800, :], cv2.COLOR_BGR2GRAY)
            gray_prev = cv2.cvtColor(prev[:800, :], cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(gray_prev, gray)
            diff = cv2.GaussianBlur(diff, (5, 5), 0)
            _, binary = cv2.threshold(diff, 50, 255, cv2.THRESH_BINARY)
            kernel = np.ones((3, 3), np.uint8)
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            for c in contours:
                area = cv2.contourArea(c)
                if 15 < area < 300:
                    perimeter = cv2.arcLength(c, True)
                    if perimeter > 0 and 4 * np.pi * area / (perimeter**2) > 0.5:
                        M = cv2.moments(c)
                        if M["m00"] > 0:
                            cx = int(M["m10"] / M["m00"])
                            cy = int(M["m01"] / M["m00"])
                            candidates.append((i, cx, cy, area))
        if ret:
            prev = frame

# Link into trajectories
frame_groups = {}
for fi, x, y, area in candidates:
    frame_groups.setdefault(fi, []).append((x, y, area))

trajectories = []
active = []
for fi in sorted(frame_groups.keys()):
    blobs = frame_groups[fi]
    used = [False] * len(blobs)
    new_active = []
    for traj in active:
        last_fi, last_x, last_y = traj[-1]
        if fi - last_fi > 16:
            if len(traj) >= 3:
                trajectories.append(traj)
            continue
        best_idx = -1
        best_dist = 60.0
        for j, (bx, by, ba) in enumerate(blobs):
            if used[j]:
                continue
            dist = ((bx - last_x) ** 2 + (by - last_y) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_idx = j
        if best_idx >= 0:
            bx, by = blobs[best_idx][0], blobs[best_idx][1]
            traj.append((fi, bx, by))
            used[best_idx] = True
            new_active.append(traj)
        else:
            if len(traj) >= 3:
                trajectories.append(traj)
    for j, (bx, by, ba) in enumerate(blobs):
        if not used[j]:
            new_active.append([(fi, bx, by)])
    active = new_active

for traj in active:
    if len(traj) >= 3:
        trajectories.append(traj)

# Classify trajectories
moving = []
static = []
for t in trajectories:
    disp = ((t[-1][1] - t[0][1]) ** 2 + (t[-1][2] - t[0][2]) ** 2) ** 0.5
    path_len = sum(
        ((t[i][1] - t[i - 1][1]) ** 2 + (t[i][2] - t[i - 1][2]) ** 2) ** 0.5
        for i in range(1, len(t))
    )
    if path_len > 30:
        moving.append((t, disp, path_len))
    else:
        static.append((t, disp, path_len))

print(f"  {len(candidates)} round small candidates in {N_FRAMES} frames")
print(f"  {len(trajectories)} trajectories (>=3 frames)")
print(f"  {len(moving)} MOVING (path_len > 30px) - likely balls")
print(f"  {len(static)} STATIC (path_len <= 30px) - likely noise")
for t, disp, pl in moving[:5]:
    print(
        f"    frames {t[0][0]}-{t[-1][0]}: {len(t)} pts, "
        f"disp={disp:.0f}px, path={pl:.0f}px, "
        f"start=({t[0][1]},{t[0][2]})"
    )

# EXPERIMENT 4: Compare with ONNX detections in same frames
print("\n=== EXP 4: Cross-reference with ONNX ===", flush=True)
# Count how many ONNX far-field labels exist per frame in this segment
seg_name = seg.stem
onnx_frames = set()
import glob as g

escaped = g.escape(seg_name)
for lf in label_dir.glob(f"{escaped}_frame_*_r0_*.txt"):
    parts = lf.stem.split("_frame_")
    if len(parts) == 2:
        fi_str = parts[1].split("_")[0]
        onnx_frames.add(int(fi_str))

print(f"  ONNX detected balls in {len(onnx_frames)} frames (far field, r0)")
print(f"  Frame diff found {len(moving)} moving trajectories")

# How many frame diff trajectories overlap with ONNX detections?
overlap = 0
diff_only = 0
for t, disp, pl in moving:
    frames_in_traj = {pt[0] for pt in t}
    if frames_in_traj & onnx_frames:
        overlap += 1
    else:
        diff_only += 1

print(f"  Overlap (both found): {overlap}")
print(f"  Frame diff ONLY (ONNX missed): {diff_only} <- NEW DETECTIONS")

cap.release()
print("\n=== DONE ===")
