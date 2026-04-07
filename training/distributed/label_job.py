"""Distributed labeling job for node agents.

Self-contained script that runs on a remote node to label one game's
video segments using the external ball detector. Writes detections to
a local manifest.db which can be merged into the server's master manifest.

Requires only:
- onnxruntime (or onnxruntime-gpu)
- opencv-python
- numpy
- The ONNX model file

Does NOT require ultralytics, torch, or the full training codebase.

Usage (on the node):
    python label_job.py \
        --video-dir "D:/videos/flash__2024.09.30_vs_Chili_home" \
        --game-id "flash__2024.09.30_vs_Chili_home" \
        --model "C:/soccer-cam-label/models/model.onnx" \
        --db "D:/labels/manifest.db" \
        --conf 0.45 \
        --frame-interval 4

    # Or receive job config from coordinator:
    python label_job.py --config job_config.json

After completion, transfer the local manifest.db to the server and merge:
    uv run python -m training.data_prep.manifest merge D:/labels/manifest.db
"""

import argparse
import json
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    print("ERROR: onnxruntime not installed. Run: pip install onnxruntime-gpu")
    exit(1)

logger = logging.getLogger(__name__)

# ---- Detection parameters ----
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5
FRAME_INTERVAL = 4

# ---- Field boundary (fisheye panoramic, 4096x1800) ----
PANO_CENTER_X = 2048.0


def field_y_far(x):
    return 310.0 + 0.0000285 * (x - PANO_CENTER_X) ** 2


def field_y_near(x):
    return 1600.0 - 0.0000220 * (x - PANO_CENTER_X) ** 2


def is_on_field(x, y, margin=50.0):
    return (field_y_far(x) - margin) <= y <= (field_y_near(x) + margin)


# ---- Tile layout ----
TILE_SIZE = 640
STEP_X = 576
STEP_Y = 580
NUM_COLS = 7
NUM_ROWS = 3


# ---- Embedded manifest schema (minimal, for label storage only) ----
LABEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    tile_count INTEGER DEFAULT 0,
    labeled_count INTEGER DEFAULT 0,
    tile_dir TEXT,
    last_updated REAL,
    tiles_cataloged REAL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    tile_stem TEXT NOT NULL,
    class_id INTEGER DEFAULT 0,
    cx REAL NOT NULL,
    cy REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    source TEXT,
    confidence REAL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_labels_game ON labels(game_id);
CREATE INDEX IF NOT EXISTS idx_labels_stem ON labels(tile_stem);
CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_unique
    ON labels(game_id, tile_stem, class_id, cx, cy);
"""


def open_label_db(db_path: Path) -> sqlite3.Connection:
    """Open or create a local manifest.db for label output."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(LABEL_SCHEMA)
    conn.commit()
    return conn


def detect_balls(frame_bgr, sess, conf_threshold=CONF_THRESHOLD):
    """Detect balls in a BGR frame at full resolution."""
    orig_h, orig_w = frame_bgr.shape[:2]
    stride = 32
    pad_h = (stride - orig_h % stride) % stride
    pad_w = (stride - orig_w % stride) % stride

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    if pad_h > 0 or pad_w > 0:
        rgb = cv2.copyMakeBorder(
            rgb, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)
        )

    blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]
    outputs = sess.run(None, {"images": blob})
    det = outputs[0][0]

    mask = det[:, 5] > conf_threshold
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(),
        filtered[:, 5].tolist(),
        conf_threshold,
        NMS_IOU_THRESHOLD,
    )

    return [
        {
            "cx": float(filtered[i][0]),
            "cy": float(filtered[i][1]),
            "w": float(filtered[i][2]),
            "h": float(filtered[i][3]),
            "conf": float(filtered[i][5]),
        }
        for i in indices
    ]


def pano_to_tile_labels(cx, cy, w, h):
    """Convert panoramic detection to per-tile YOLO labels."""
    labels = []
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            tile_x0 = col * STEP_X
            tile_y0 = row * STEP_Y
            tcx = cx - tile_x0
            tcy = cy - tile_y0
            if 0 <= tcx < TILE_SIZE and 0 <= tcy < TILE_SIZE:
                labels.append(
                    {
                        "row": row,
                        "col": col,
                        "cx_norm": tcx / TILE_SIZE,
                        "cy_norm": tcy / TILE_SIZE,
                        "w_norm": w / TILE_SIZE,
                        "h_norm": h / TILE_SIZE,
                    }
                )
    return labels


def _collect_frame_labels(seg_name, frame_idx, dets):
    """Convert detections to manifest label rows.

    Returns list of (tile_stem, class_id, cx, cy, w, h, confidence).
    """
    rows = []
    for det in dets:
        for tl in pano_to_tile_labels(det["cx"], det["cy"], det["w"], det["h"]):
            tile_stem = f"{seg_name}_frame_{frame_idx:06d}_r{tl['row']}_c{tl['col']}"
            rows.append((
                tile_stem,
                0,  # class_id (ball)
                tl["cx_norm"],
                tl["cy_norm"],
                tl["w_norm"],
                tl["h_norm"],
                det["conf"],
            ))
    return rows


def process_segment(
    video_path,
    sess,
    conn,
    game_id,
    frame_interval=FRAME_INTERVAL,
    conf_threshold=CONF_THRESHOLD,
):
    """Process one video segment and write labels to manifest.db.

    GPU detection runs frame-by-frame; labels are batch-inserted into
    SQLite after each segment completes.
    """
    seg_name = video_path.stem
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return 0

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info("  %s (%d frames, interval=%d)", seg_name[:50], total, frame_interval)

    fi = 0
    det_count = 0
    all_rows = []  # collect all label rows for batch insert
    start = time.time()

    while True:
        ret = cap.grab()
        if not ret:
            break
        if fi % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret:
                dets = detect_balls(frame, sess, conf_threshold)
                det_count += len(dets)
                if dets:
                    all_rows.extend(_collect_frame_labels(seg_name, fi, dets))
        fi += 1

    cap.release()

    # Batch insert into manifest
    if all_rows:
        conn.executemany(
            """INSERT OR IGNORE INTO labels
               (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(game_id, stem, cls, cx, cy, w, h, "onnx", conf)
             for stem, cls, cx, cy, w, h, conf in all_rows],
        )
        conn.commit()

    elapsed = time.time() - start
    logger.info(
        "    %d detections -> %d label rows in %.0fs", det_count, len(all_rows), elapsed
    )
    return len(all_rows)


def run_label_job(
    video_dir,
    game_id,
    model_path,
    db_path,
    conf=CONF_THRESHOLD,
    frame_interval=FRAME_INTERVAL,
    use_gpu=True,
):
    """Run labeling on all segments in a video directory."""
    providers = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
        providers.append("DmlExecutionProvider")
    providers.append("CPUExecutionProvider")

    sess = ort.InferenceSession(str(model_path), providers=providers)
    actual = sess.get_providers()
    logger.info("ONNX providers: %s", actual)

    # Open local manifest for output
    conn = open_label_db(db_path)

    # Ensure game row exists
    conn.execute(
        "INSERT OR IGNORE INTO games (game_id, last_updated) VALUES (?, ?)",
        (game_id, time.time()),
    )
    conn.commit()

    # Search for segment videos
    segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
    if not segments:
        logger.error("No segments found in %s", video_dir)
        conn.close()
        return 0

    # Check which segments already have labels in our manifest
    existing_stems = set()
    rows = conn.execute(
        "SELECT DISTINCT tile_stem FROM labels WHERE game_id = ?", (game_id,)
    ).fetchall()
    for (stem,) in rows:
        # Extract segment name from tile_stem (everything before _frame_)
        parts = stem.rsplit("_frame_", 1)
        if parts:
            existing_stems.add(parts[0])

    unlabeled = []
    for seg in segments:
        if seg.stem in existing_stems:
            logger.info("  Skipping %s (labels exist in manifest)", seg.stem[:50])
        else:
            unlabeled.append(seg)

    logger.info(
        "=== %s: %d segments (%d already labeled, %d to do) ===",
        game_id,
        len(segments),
        len(segments) - len(unlabeled),
        len(unlabeled),
    )

    total_rows = 0
    for seg in unlabeled:
        total_rows += process_segment(
            seg, sess, conn, game_id, frame_interval, conf,
        )

    # Update game metadata
    labeled_count = conn.execute(
        "SELECT COUNT(DISTINCT tile_stem) FROM labels WHERE game_id = ?", (game_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE games SET labeled_count = ?, last_updated = ? WHERE game_id = ?",
        (labeled_count, time.time(), game_id),
    )
    conn.commit()
    conn.close()

    logger.info("=== DONE: %d label rows written to %s ===", total_rows, db_path)
    return total_rows


def main():
    parser = argparse.ArgumentParser(description="Run ball detection labeling job")
    parser.add_argument(
        "--video-dir", type=Path, help="Directory with segment .mp4 files"
    )
    parser.add_argument(
        "--game-id", type=str, help="Game ID for manifest labels"
    )
    parser.add_argument("--model", type=Path, default=Path("model.onnx"))
    parser.add_argument(
        "--db", type=Path, default=Path("manifest.db"),
        help="Local manifest.db to write labels into",
    )
    parser.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    parser.add_argument("--frame-interval", type=int, default=FRAME_INTERVAL)
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    parser.add_argument(
        "--config", type=Path, help="Job config JSON (overrides other args)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        args.video_dir = Path(cfg["video_dir"])
        args.game_id = cfg["game_id"]
        args.model = Path(cfg["model"])
        args.db = Path(cfg.get("db", "manifest.db"))
        args.conf = cfg.get("conf", CONF_THRESHOLD)
        args.frame_interval = cfg.get("frame_interval", FRAME_INTERVAL)
        args.cpu = cfg.get("cpu", False)

    if not args.game_id:
        # Infer game_id from video_dir name
        args.game_id = args.video_dir.name

    run_label_job(
        args.video_dir,
        args.game_id,
        args.model,
        args.db,
        args.conf,
        args.frame_interval,
        use_gpu=not args.cpu,
    )


if __name__ == "__main__":
    main()
