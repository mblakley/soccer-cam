"""Auto-detect game phases from label data.

Scans labels to estimate warmup/active/halftime/postgame boundaries
based on detection patterns. Outputs per-game manifest with phase
guesses for human confirmation.

Phases:
  - warmup: multiple balls, scattered, before kickoff
  - first_half: single game ball, active play
  - halftime: gap in play, multiple balls again
  - second_half: single game ball, active play
  - postgame: multiple balls, players leaving

Detection signals:
  - Multiple concurrent detections (>3 spread positions) = non-game
  - Single consistent trajectory = active play
  - Detection gap (>2 min no detections) = phase boundary
"""

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import _parse_detection, _tile_to_pano

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

LABELS_DIR = Path("F:/training_data/labels_640_ext")
OUTPUT_DIR = Path("F:/training_data/game_manifests")

# Video share paths for linking
SHARE = "//192.168.86.152/video"
GAME_VIDEO_DIRS = {
    "flash__06.01.2024_vs_IYSA_home": f"{SHARE}/Flash_2013s/06.01.2024 - vs IYSA (home)",
    "flash__09.27.2024_vs_RNYFC_Black_home": f"{SHARE}/Flash_2013s/09.27.2024 - vs RNYFC Black (home)",
    "flash__09.30.2024_vs_Chili_home": f"{SHARE}/Flash_2013s/09.30.2024 - vs Chili (home)",
    "flash__2025.06.02": f"{SHARE}/Flash_2013s/2025.06.02-18.16.03",
    "heat__05.31.2024_vs_Fairport_home": f"{SHARE}/Heat_2012s/05.31.2024 - vs Fairport (home)",
    "heat__06.20.2024_vs_Chili_away": f"{SHARE}/Heat_2012s/06.20.2024 - vs Chili (away)",
    "heat__07.17.2024_vs_Fairport_away": f"{SHARE}/Heat_2012s/07.17.2024 - vs Fairport (away)",
    "heat__Clarence_Tournament": f"{SHARE}/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament",
    "heat__Heat_Tournament": f"{SHARE}/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament",
}

# Assume 30fps video, labels every 4 frames
FPS = 30
LABEL_INTERVAL = 4
WINDOW_FRAMES = 450  # 30-second window (450 frames at 30fps)
MULTI_BALL_THRESHOLD = 3  # concurrent detections spread > 500px apart
SPREAD_THRESHOLD = 500  # px in panoramic coords to count as "spread"
GAP_THRESHOLD_FRAMES = 3600  # 2 min gap = phase boundary (3600 frames at 30fps)


def parse_segment_time(segment_name: str) -> tuple[int, int] | None:
    """Extract start hour/minute from segment name like '18.01.30-18.18.20[F]...'"""
    try:
        parts = segment_name.split("-")
        start = parts[0]
        h, m, s = int(start[:2]), int(start[3:5]), int(start[6:8])
        return h * 3600 + m * 60 + s, 0
    except (ValueError, IndexError):
        return None


def detect_phases(game_id: str) -> dict:
    """Detect game phases from label data."""
    label_dir = LABELS_DIR / game_id
    if not label_dir.exists():
        return {}

    # Parse all detections with timestamps
    # Key: (segment, frame_idx) -> list of (pano_x, pano_y)
    frame_dets: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    segments = set()

    for lf in label_dir.glob("*.txt"):
        parsed = parse_tile_filename(lf.stem)
        if not parsed:
            continue
        seg, fi, row, col = parsed
        segments.add(seg)
        for cx, cy, _ in _parse_detection(lf):
            px, py = _tile_to_pano(cx, cy, row, col)
            frame_dets[(seg, fi)].append((px, py))

    if not frame_dets:
        return {}

    # Build timeline: for each segment, compute detection count per window
    segment_info = {}
    for seg in sorted(segments):
        seg_frames = {fi for s, fi in frame_dets if s == seg}
        if not seg_frames:
            continue

        seg_time = parse_segment_time(seg)
        min_fi = min(seg_frames)
        max_fi = max(seg_frames)

        # Count "multi-ball" frames in rolling windows
        windows = []
        for start_fi in range(min_fi, max_fi, WINDOW_FRAMES // 2):
            end_fi = start_fi + WINDOW_FRAMES
            window_frames = [fi for fi in seg_frames if start_fi <= fi < end_fi]

            multi_ball_count = 0
            total_dets = 0
            for fi in window_frames:
                dets = frame_dets[(seg, fi)]
                total_dets += len(dets)
                if len(dets) >= MULTI_BALL_THRESHOLD:
                    # Check if detections are spread apart
                    if len(dets) >= 2:
                        xs = [d[0] for d in dets]
                        spread = max(xs) - min(xs)
                        if spread > SPREAD_THRESHOLD:
                            multi_ball_count += 1

            window_time_sec = (start_fi - min_fi) / FPS
            if seg_time:
                abs_time = seg_time[0] + window_time_sec
            else:
                abs_time = window_time_sec

            windows.append(
                {
                    "start_frame": start_fi,
                    "time_sec": round(abs_time),
                    "time_str": f"{int(abs_time // 3600):02d}:{int((abs_time % 3600) // 60):02d}:{int(abs_time % 60):02d}",
                    "det_count": total_dets,
                    "multi_ball_pct": round(
                        multi_ball_count / max(len(window_frames), 1), 2
                    ),
                    "frame_count": len(window_frames),
                }
            )

        segment_info[seg] = {
            "start_time": seg_time[0] if seg_time else 0,
            "frame_range": [min_fi, max_fi],
            "windows": windows,
        }

    # Guess phases based on multi-ball patterns
    # Simple heuristic: first stretch of low multi-ball = first half,
    # gap = halftime, second stretch = second half
    all_windows = []
    for seg in sorted(segment_info):
        for w in segment_info[seg]["windows"]:
            w["segment"] = seg
            all_windows.append(w)
    all_windows.sort(key=lambda w: w["time_sec"])

    # Find phase transitions
    phases = []
    current_phase = "unknown"
    phase_start = all_windows[0]["time_sec"] if all_windows else 0

    for i, w in enumerate(all_windows):
        is_multi = w["multi_ball_pct"] > 0.15  # >15% of window has multi-ball
        is_gap = w["frame_count"] < 5  # very few detections

        if current_phase == "unknown":
            if is_multi:
                current_phase = "warmup"
                phase_start = w["time_sec"]
            else:
                current_phase = "first_half"
                phase_start = w["time_sec"]

        elif current_phase == "warmup":
            if not is_multi and not is_gap:
                phases.append(
                    {
                        "phase": "warmup",
                        "start_sec": phase_start,
                        "end_sec": w["time_sec"],
                        "start_str": _fmt_time(phase_start),
                        "end_str": _fmt_time(w["time_sec"]),
                        "confirmed": False,
                    }
                )
                current_phase = "first_half"
                phase_start = w["time_sec"]

        elif current_phase == "first_half":
            if is_gap or (is_multi and i > len(all_windows) * 0.3):
                phases.append(
                    {
                        "phase": "first_half",
                        "start_sec": phase_start,
                        "end_sec": w["time_sec"],
                        "start_str": _fmt_time(phase_start),
                        "end_str": _fmt_time(w["time_sec"]),
                        "confirmed": False,
                    }
                )
                current_phase = "halftime"
                phase_start = w["time_sec"]

        elif current_phase == "halftime":
            if not is_multi and not is_gap:
                phases.append(
                    {
                        "phase": "halftime",
                        "start_sec": phase_start,
                        "end_sec": w["time_sec"],
                        "start_str": _fmt_time(phase_start),
                        "end_str": _fmt_time(w["time_sec"]),
                        "confirmed": False,
                    }
                )
                current_phase = "second_half"
                phase_start = w["time_sec"]

        elif current_phase == "second_half":
            if is_multi and i > len(all_windows) * 0.7:
                phases.append(
                    {
                        "phase": "second_half",
                        "start_sec": phase_start,
                        "end_sec": w["time_sec"],
                        "start_str": _fmt_time(phase_start),
                        "end_str": _fmt_time(w["time_sec"]),
                        "confirmed": False,
                    }
                )
                current_phase = "postgame"
                phase_start = w["time_sec"]

    # Close final phase
    if all_windows and current_phase != "unknown":
        phases.append(
            {
                "phase": current_phase,
                "start_sec": phase_start,
                "end_sec": all_windows[-1]["time_sec"],
                "start_str": _fmt_time(phase_start),
                "end_str": _fmt_time(all_windows[-1]["time_sec"]),
                "confirmed": False,
            }
        )

    # Build game manifest
    video_dir = GAME_VIDEO_DIRS.get(game_id, "")
    video_segments = []
    for seg in sorted(segments):
        info = segment_info.get(seg, {})
        video_segments.append(
            {
                "segment": seg,
                "start_time": info.get("start_time", 0),
                "start_str": _fmt_time(info.get("start_time", 0)),
                "video_link": "https://trainer.goat-rattlesnake.ts.net:8642/api/tracking-lab/tile/0?row=1&col=3",
            }
        )

    return {
        "game_id": game_id,
        "video_dir": video_dir,
        "orientation": "upside_down"
        if game_id
        in {
            "flash__06.01.2024_vs_IYSA_home",
            "heat__05.31.2024_vs_Fairport_home",
        }
        else "right_side_up",
        "segments": video_segments,
        "phases": phases,
        "events": [],  # user adds: goals, corners, PKs, etc.
        "notes": "",
        "auto_detected": True,
        "confirmed_by": None,
    }


def _fmt_time(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start = time.time()

    games = sorted(GAME_VIDEO_DIRS.keys())

    for game_id in games:
        manifest = detect_phases(game_id)
        if not manifest:
            logger.warning("No data for %s", game_id)
            continue

        out_path = OUTPUT_DIR / f"{game_id}.json"
        with open(out_path, "w") as f:
            json.dump(manifest, f, indent=2)

        phase_summary = ", ".join(
            f"{p['phase']}({p['start_str']}-{p['end_str']})" for p in manifest["phases"]
        )
        logger.info("%s: %s", game_id, phase_summary)

    elapsed = time.time() - start
    logger.info("Done in %.0fs. Manifests: %s", elapsed, OUTPUT_DIR)


if __name__ == "__main__":
    main()
