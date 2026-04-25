"""FastAPI annotation server for mobile review over Tailscale.

Runs on the training PC, serves review packet crops and collects
annotation results from the Flutter app. No auth needed -- Tailscale
provides the private network boundary.

Usage:
    uvicorn training.annotation_server:app --host 0.0.0.0 --port 8642
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Configurable via environment or CLI args
REVIEW_PACKETS_DIR = Path("D:/training_data/review_packets")
LABELS_OUTPUT_DIR = Path("training_data/labels/annotations")

app = FastAPI(title="Ball Tracking Annotation Server", version="0.1.0")


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Prevent browsers from caching static assets (HTML/JS/CSS)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/sw.js":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheStaticMiddleware)


# --- Request/Response models ---


class AnnotationResult(BaseModel):
    model_config = {"extra": "allow"}

    frame_idx: int
    action: str  # confirm, reject, adjust, locate, not_visible, skip
    ball_position: dict | None = None  # {"x": int, "y": int}
    duration_ms: int | None = None
    auto_skipped: bool | None = None
    warmup: bool | None = None
    game_over: bool | None = None


class PacketResults(BaseModel):
    results: list[AnnotationResult]


class PacketSummary(BaseModel):
    game_id: str
    frame_count: int
    reviewed_count: int
    status: str  # "pending", "partial", "complete"


# --- Helpers ---


def _get_packets_dir() -> Path:
    """Get the review packets directory, creating if needed."""
    REVIEW_PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    return REVIEW_PACKETS_DIR


def _load_manifest(game_id: str) -> dict:
    """Load manifest.json for a game packet."""
    manifest_path = _get_packets_dir() / game_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"Packet not found: {game_id}")
    with open(manifest_path) as f:
        return json.load(f)


def _load_results(game_id: str) -> list[dict]:
    """Load existing annotation results for a game packet."""
    results_path = _get_packets_dir() / game_id / "annotation_results.json"
    if not results_path.exists():
        return []
    with open(results_path) as f:
        return json.load(f)


def _save_results(game_id: str, results: list[dict]) -> None:
    """Save annotation results for a game packet."""
    results_path = _get_packets_dir() / game_id / "annotation_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def _packet_status(manifest: dict, results: list[dict]) -> str:
    """Determine review status of a packet."""
    total = len(manifest.get("frames", []))
    reviewed = len(results)
    if reviewed == 0:
        return "pending"
    elif reviewed >= total:
        return "complete"
    return "partial"


# --- Endpoints ---


@app.get("/api/packets", response_model=list[PacketSummary])
async def list_packets():
    """List all available review packets with their status."""
    packets_dir = _get_packets_dir()
    summaries = []

    for entry in sorted(packets_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        results = _load_results(entry.name)
        status = _packet_status(manifest, results)

        summaries.append(
            PacketSummary(
                game_id=entry.name,
                frame_count=len(manifest.get("frames", [])),
                reviewed_count=len(results),
                status=status,
            )
        )

    return summaries


@app.get("/api/packets/{game_id}")
async def get_packet(game_id: str):
    """Get the full manifest for a review packet."""
    manifest = _load_manifest(game_id)
    results = _load_results(game_id)

    reviewed_indices = {r["frame_idx"] for r in results}

    return {
        **manifest,
        "reviewed_count": len(results),
        "status": _packet_status(manifest, results),
        "reviewed_frames": sorted(reviewed_indices),
    }


@app.get("/api/packets/{game_id}/crops/{frame_idx}")
async def get_crop(game_id: str, frame_idx: int):
    """Serve a crop image for a specific frame."""
    manifest = _load_manifest(game_id)

    frame_entry = None
    for f in manifest.get("frames", []):
        if f["frame_idx"] == frame_idx:
            frame_entry = f
            break

    if frame_entry is None:
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} not found")

    crop_path = _get_packets_dir() / game_id / frame_entry["crop_file"]
    if not crop_path.exists():
        raise HTTPException(status_code=404, detail="Crop image file not found")

    return FileResponse(crop_path, media_type="image/jpeg")


@app.post("/api/packets/{game_id}/results")
async def submit_results(game_id: str, packet_results: PacketResults):
    """Submit annotation results for a review packet.

    Merges with existing results (supports incremental submission).
    """
    _load_manifest(game_id)  # validate packet exists

    existing = _load_results(game_id)
    existing_by_frame = {r["frame_idx"]: r for r in existing}

    for result in packet_results.results:
        entry = {
            "frame_idx": result.frame_idx,
            "action": result.action,
            "ball_position": result.ball_position,
            "duration_ms": result.duration_ms,
            "reviewer": "phone",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        if result.auto_skipped:
            entry["auto_skipped"] = True
        if result.warmup:
            entry["warmup"] = True
        if result.game_over:
            entry["game_over"] = True
        existing_by_frame[result.frame_idx] = entry

    merged = sorted(existing_by_frame.values(), key=lambda r: r["frame_idx"])
    _save_results(game_id, merged)

    manifest = _load_manifest(game_id)
    total = len(manifest.get("frames", []))

    return {
        "accepted": len(packet_results.results),
        "total_reviewed": len(merged),
        "total_frames": total,
        "status": _packet_status(manifest, merged),
    }


@app.post("/api/packets/{game_id}/skip")
async def skip_packet(game_id: str):
    """Mark a packet as skipped/deferred."""
    _load_manifest(game_id)  # validate exists

    skip_marker = _get_packets_dir() / game_id / ".skipped"
    skip_marker.touch()

    return {"game_id": game_id, "status": "skipped"}


@app.get("/api/stats")
async def get_stats():
    """Get aggregate annotation statistics."""
    packets_dir = _get_packets_dir()

    total_packets = 0
    total_frames = 0
    total_reviewed = 0
    action_counts: dict[str, int] = {}
    total_duration_ms = 0

    for entry in sorted(packets_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        total_packets += 1
        total_frames += len(manifest.get("frames", []))

        results = _load_results(entry.name)
        total_reviewed += len(results)

        for r in results:
            action = r.get("action", "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
            if r.get("duration_ms"):
                total_duration_ms += r["duration_ms"]

    confirmed = action_counts.get("confirm", 0)
    total_with_opinion = total_reviewed - action_counts.get("skip", 0)
    agreement_rate = confirmed / total_with_opinion if total_with_opinion > 0 else 0.0

    return {
        "total_packets": total_packets,
        "total_frames": total_frames,
        "total_reviewed": total_reviewed,
        "review_rate": f"{total_reviewed}/{total_frames}"
        if total_frames > 0
        else "0/0",
        "agreement_rate": round(agreement_rate, 3),
        "action_breakdown": action_counts,
        "total_review_time_minutes": round(total_duration_ms / 60000, 1),
        "avg_ms_per_frame": (
            round(total_duration_ms / total_reviewed) if total_reviewed > 0 else 0
        ),
    }


@app.get("/api/exclusions")
async def get_exclusions():
    """Return learned exclusions with sample images for review.

    Groups exclusions into:
    - warmup_ranges: per-game time cutoffs with sample crop images
    - static_balls: per-position ball clusters with sample crop images
    """
    packets_dir = _get_packets_dir()
    warmup_frames: dict[str, list] = {}  # game_id -> list of frame info
    gameover_frames: dict[str, list] = {}  # game_id -> list of frame info
    static_ball_clusters: dict[str, list] = {}  # "game_id/r{row}c{col}" -> list

    for packet_dir in sorted(packets_dir.iterdir()):
        if not packet_dir.is_dir() or not packet_dir.name.startswith("tracking_loss_"):
            continue

        manifest_path = packet_dir / "manifest.json"
        results_path = packet_dir / "annotation_results.json"
        if not manifest_path.exists() or not results_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)
        with open(results_path) as f:
            results = json.load(f)

        frame_lookup = {fr["frame_idx"]: fr for fr in manifest["frames"]}
        results_by_frame = {r["frame_idx"]: r for r in results}

        for frame_idx, result in results_by_frame.items():
            frame = frame_lookup.get(frame_idx)
            if not frame:
                continue

            ctx = frame.get("context", {})
            game_id = ctx.get("game_id", "")
            action = result.get("action", "")
            packet_id = packet_dir.name

            det = frame.get("model_detection") or {}
            det_x = int(det.get("x", -1))
            det_y = int(det.get("y", -1))
            thumb_url = (
                f"/api/packets/{packet_id}/thumb/{frame_idx}?bx={det_x}&by={det_y}"
            )

            frame_info = {
                "packet_id": packet_id,
                "frame_idx": frame_idx,
                "thumb_url": thumb_url,
                "time_secs": ctx.get("time_secs", 0),
                "pct_through": ctx.get("pct_through", 0),
                "row": ctx.get("row"),
                "col": ctx.get("col"),
            }

            is_warmup = result.get("warmup") or (
                action == "skip" and result.get("auto_skipped")
            )

            is_gameover = result.get("game_over")

            if is_warmup:
                warmup_frames.setdefault(game_id, []).append(frame_info)
            elif is_gameover:
                gameover_frames.setdefault(game_id, []).append(frame_info)
            elif action == "not_game_ball":
                key = f"{game_id}/r{ctx.get('row', '?')}c{ctx.get('col', '?')}"
                static_ball_clusters.setdefault(key, []).append(frame_info)

    # Build warmup summary
    warmup_ranges = []
    for game_id, wf in sorted(warmup_frames.items()):
        wf.sort(key=lambda x: x["time_secs"])
        max_time = max(f["time_secs"] for f in wf)
        warmup_ranges.append(
            {
                "game_id": game_id,
                "cutoff_secs": max_time,
                "cutoff_mins": round(max_time / 60, 1),
                "frame_count": len(wf),
                "samples": wf[:6],  # show up to 6 sample images
            }
        )

    # Build game-over summary
    gameover_ranges = []
    for game_id, gf in sorted(gameover_frames.items()):
        gf.sort(key=lambda x: x["time_secs"])
        min_time = min(f["time_secs"] for f in gf)
        gameover_ranges.append(
            {
                "game_id": game_id,
                "cutoff_secs": min_time,
                "cutoff_mins": round(min_time / 60, 1),
                "frame_count": len(gf),
                "samples": gf[:6],
            }
        )

    # Build static ball summary — cluster by position proximity
    static_balls = []
    for key, sf in sorted(static_ball_clusters.items()):
        sf.sort(key=lambda x: x["time_secs"])
        static_balls.append(
            {
                "position_key": key,
                "frame_count": len(sf),
                "time_range": {
                    "min_secs": sf[0]["time_secs"],
                    "max_secs": sf[-1]["time_secs"],
                },
                "samples": sf[:6],
            }
        )

    # Count total exclusions that would apply
    total_excluded = (
        sum(r["frame_count"] for r in warmup_ranges)
        + sum(r["frame_count"] for r in gameover_ranges)
        + sum(s["frame_count"] for s in static_balls)
    )

    return {
        "warmup_ranges": warmup_ranges,
        "gameover_ranges": gameover_ranges,
        "static_balls": static_balls,
        "total_annotated_exclusions": total_excluded,
    }


@app.get("/api/packets/{game_id}/thumb/{frame_idx}")
async def get_thumb(game_id: str, frame_idx: int, bx: int = -1, by: int = -1):
    """Serve an 80x80 thumbnail with an arrow pointing at the ball position."""
    import io
    import math

    from PIL import Image, ImageDraw
    from starlette.responses import Response

    manifest = _load_manifest(game_id)

    frame_entry = None
    for f in manifest.get("frames", []):
        if f["frame_idx"] == frame_idx:
            frame_entry = f
            break
    if frame_entry is None:
        raise HTTPException(status_code=404, detail=f"Frame {frame_idx} not found")

    crop_path = _get_packets_dir() / game_id / frame_entry["crop_file"]
    if not crop_path.exists():
        raise HTTPException(status_code=404, detail="Crop image not found")

    S = 80
    img = Image.open(crop_path).resize((S, S), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    if bx >= 0 and by >= 0:
        # Scale from 640x640 to 80x80
        tx = bx * S / 640
        ty = by * S / 640

        # Arrow from furthest corner
        fx = S - 5 if tx < S / 2 else 5
        fy = S - 5 if ty < S / 2 else 5

        dx, dy = tx - fx, ty - fy
        length = math.sqrt(dx * dx + dy * dy)
        if length > 0:
            ux, uy = dx / length, dy / length
            # Tip stops 5px from ball
            tip_x = tx - ux * 5
            tip_y = ty - uy * 5

            # Shaft
            draw.line([(fx, fy), (tip_x, tip_y)], fill="#ff3333", width=3)

            # Arrowhead
            angle = math.atan2(dy, dx)
            hl = 10
            ha = 0.45
            pts = [
                (tip_x, tip_y),
                (
                    tip_x - hl * math.cos(angle - ha),
                    tip_y - hl * math.sin(angle - ha),
                ),
                (
                    tip_x - hl * math.cos(angle + ha),
                    tip_y - hl * math.sin(angle + ha),
                ),
            ]
            draw.polygon(pts, fill="#ff3333")

        # Circle at ball
        r = 4
        draw.ellipse([tx - r, ty - r, tx + r, ty + r], outline="#ff3333", width=2)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@app.get("/api/generate-progress")
async def generation_progress():
    """Return current packet generation progress (if any)."""
    progress_file = _get_packets_dir() / ".generation_progress.json"
    if not progress_file.exists():
        return {"active": False}
    try:
        data = json.loads(progress_file.read_text())
        # Stale if older than 5 minutes
        if time.time() - data.get("timestamp", 0) > 300:
            return {"active": False}
        return {"active": data.get("phase") != "done", **data}
    except (json.JSONDecodeError, OSError):
        return {"active": False}


@app.post("/api/generate-packet")
async def generate_packet():
    """Generate the next tracking loss packet using learned exclusions.

    Reads annotations from completed packets to learn warmup cutoffs
    and static ball positions, then generates a filtered packet.
    """
    from training.annotation.tracking_loss_generator import (
        generate_next_tracking_loss_packet,
    )

    manifest = await asyncio.to_thread(
        generate_next_tracking_loss_packet,
        dataset_path=Path("F:/training_data/ball_dataset_640"),
        tiles_path=Path("F:/training_data/tiles_640"),
        output_dir=REVIEW_PACKETS_DIR,
        packet_size=100,
    )

    if manifest:
        packet_id = manifest.parent.name
        return {"status": "created", "packet_id": packet_id}
    return {"status": "exhausted", "packet_id": None}


@app.post("/api/ingest")
async def trigger_ingestion():
    """Trigger ingestion of completed annotation results into YOLO labels.

    Only processes packets with annotation_results.json that haven't been
    ingested yet.
    """
    from training.correction_ingester import ingest_all_packets

    stats_list = await asyncio.to_thread(
        ingest_all_packets, REVIEW_PACKETS_DIR, LABELS_OUTPUT_DIR
    )

    return JSONResponse(
        {
            "packets_processed": len(stats_list),
            "total_labels_written": sum(s.labels_written for s in stats_list),
        }
    )


# --- Tracking Lab endpoints ---

TRACKING_LAB_DIR = REVIEW_PACKETS_DIR / "tracking_lab"


@app.get("/api/tracking-lab")
async def get_tracking_lab():
    """Return the tracking lab manifest."""
    manifest_path = TRACKING_LAB_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "No tracking lab session found")
    with open(manifest_path) as f:
        return json.load(f)


@app.get("/api/tracking-lab/tile/{frame_idx}")
async def get_tracking_lab_tile(frame_idx: int, row: int, col: int):
    """Serve a tile image for the tracking lab."""
    manifest_path = TRACKING_LAB_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "No tracking lab session")
    with open(manifest_path) as f:
        manifest = json.load(f)

    game_id = manifest["game_id"]
    segment = manifest["segment"]
    tiles_root = Path("F:/training_data/tiles_640") / game_id

    # Find the tile file (.jpg or .excluded for row 0)
    base = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
    tile_path = tiles_root / (base + ".jpg")
    if not tile_path.exists():
        tile_path = tiles_root / (base + ".excluded")
    if not tile_path.exists():
        raise HTTPException(404, f"Tile not found: {base}")
    return FileResponse(
        tile_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/tracking-lab/tiles/{frame_idx}")
async def get_tracking_lab_tiles_for_frame(frame_idx: int):
    """List all available tiles for a given frame index."""
    manifest_path = TRACKING_LAB_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "No tracking lab session")
    with open(manifest_path) as f:
        manifest = json.load(f)

    game_id = manifest["game_id"]
    segment = manifest["segment"]
    tiles_root = Path("F:/training_data/tiles_640") / game_id

    tiles = []
    for row in range(3):  # r0, r1, r2 (include r0 for tracking lab)
        for col in range(7):  # c0-c6
            base = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
            if (tiles_root / (base + ".jpg")).exists() or (
                tiles_root / (base + ".excluded")
            ).exists():
                tiles.append({"row": row, "col": col})
    return tiles


@app.post("/api/tracking-lab/feedback")
async def submit_tracking_lab_feedback(feedback: dict):
    """Save user feedback for a tracking lab frame."""
    feedback_path = TRACKING_LAB_DIR / "feedback.json"
    existing = []
    if feedback_path.exists():
        with open(feedback_path) as f:
            existing = json.load(f)

    existing.append(feedback)
    with open(feedback_path, "w") as f:
        json.dump(existing, f, indent=2)

    return {"status": "saved", "total_feedback": len(existing)}


@app.get("/api/tracking-lab/feedback")
async def get_tracking_lab_feedback():
    """Return all feedback for the tracking lab."""
    feedback_path = TRACKING_LAB_DIR / "feedback.json"
    if not feedback_path.exists():
        return []
    with open(feedback_path) as f:
        return json.load(f)


@app.get("/api/tracking-lab/messages")
async def get_lab_messages():
    """Return all messages from the tracking lab chat."""
    msg_path = TRACKING_LAB_DIR / "messages.json"
    if not msg_path.exists():
        return []
    with open(msg_path) as f:
        return json.load(f)


@app.post("/api/tracking-lab/messages")
async def post_lab_message(message: dict):
    """Save a message to the tracking lab chat."""
    msg_path = TRACKING_LAB_DIR / "messages.json"
    existing = []
    if msg_path.exists():
        with open(msg_path) as f:
            existing = json.load(f)

    existing.append(
        {
            "text": message.get("text", ""),
            "frame_idx": message.get("frame_idx"),
            "tile": message.get("tile"),
            "timestamp": time.time(),
        }
    )
    with open(msg_path, "w") as f:
        json.dump(existing, f, indent=2)

    return {"status": "saved", "total": len(existing)}


@app.post("/api/tracking-lab/regenerate")
async def regenerate_tracking_lab():
    """Regenerate the tracking lab manifest using current feedback."""
    manifest_path = TRACKING_LAB_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "No tracking lab session")
    with open(manifest_path) as f:
        old = json.load(f)

    from training.annotation.tracking_lab import build_tracking_lab

    game_id = old["game_id"]
    segment = old.get("segment_prefix", old["segment"][:17])

    # Find external detections file
    det_files = sorted(Path("F:/training_data").glob("ext_detections_*_clean.json"))
    ext_det = det_files[-1] if det_files else None

    result = await asyncio.to_thread(
        build_tracking_lab,
        tiles_dir=Path("F:/training_data/tiles_640") / game_id,
        labels_dir=Path("F:/training_data/labels_640_filtered") / game_id,
        game_id=game_id,
        segment_prefix=segment,
        output_dir=TRACKING_LAB_DIR,
        external_detections=ext_det,
    )

    if result:
        with open(result) as f:
            manifest = json.load(f)
        return {
            "status": "regenerated",
            "tracked_frames": manifest["tracked_frames"],
            "total_frames": manifest["total_frames"],
            "coverage_pct": manifest["coverage_pct"],
        }
    return {"status": "failed"}


# --- Ball Verification endpoints ---

BALL_VERIFY_DIR = REVIEW_PACKETS_DIR / "ball_verify"


@app.get("/api/ball-verify")
async def get_ball_verify():
    """Get the ball verification manifest."""
    manifest_path = BALL_VERIFY_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "No ball verification packet found")
    with open(manifest_path) as f:
        manifest = json.load(f)

    results = _load_ball_verify_results()
    reviewed_ids = {r["frame_idx"] for r in results}
    unreviewed = [f for f in manifest["frames"] if f["frame_idx"] not in reviewed_ids]

    return {
        **manifest,
        "reviewed_count": len(results),
        "remaining_count": len(unreviewed),
        "next_frame": unreviewed[0] if unreviewed else None,
        "stats": _ball_verify_stats(results),
    }


@app.get("/api/ball-verify/crop/{candidate_id}")
async def get_ball_verify_crop(candidate_id: int):
    """Serve the cropped image for a candidate."""
    crop_path = BALL_VERIFY_DIR / "crops" / f"crop_{candidate_id:05d}.jpg"
    if not crop_path.exists():
        raise HTTPException(404, "Crop not found")
    return FileResponse(crop_path, media_type="image/jpeg")


@app.get("/api/ball-verify/full/{candidate_id}")
async def get_ball_verify_full(candidate_id: int):
    """Serve the full panoramic frame for a candidate."""
    full_path = BALL_VERIFY_DIR / "full_frames" / f"full_{candidate_id:05d}.jpg"
    if not full_path.exists():
        raise HTTPException(404, "Full frame not found")
    return FileResponse(full_path, media_type="image/jpeg")


@app.post("/api/ball-verify/result")
async def submit_ball_verify_result(result: dict):
    """Submit a verification result for one candidate.

    Expected: {"frame_idx": int, "verdict": "ball"|"not_ball"|"unclear"}
    """
    results = _load_ball_verify_results()
    results = [r for r in results if r["frame_idx"] != result["frame_idx"]]
    results.append(
        {
            "frame_idx": result["frame_idx"],
            "verdict": result["verdict"],
            "notes": result.get("notes", ""),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    results.sort(key=lambda r: r["frame_idx"])
    _save_ball_verify_results(results)

    return {
        "accepted": True,
        "total_reviewed": len(results),
        "stats": _ball_verify_stats(results),
    }


def _load_ball_verify_results() -> list[dict]:
    results_path = BALL_VERIFY_DIR / "verification_results.json"
    if not results_path.exists():
        return []
    with open(results_path) as f:
        return json.load(f)


def _save_ball_verify_results(results: list[dict]):
    results_path = BALL_VERIFY_DIR / "verification_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def _ball_verify_stats(results: list[dict]) -> dict:
    ball = sum(1 for r in results if r["verdict"] == "ball")
    not_ball = sum(1 for r in results if r["verdict"] == "not_ball")
    unclear = sum(1 for r in results if r["verdict"] == "unclear")
    total = len(results)
    return {
        "ball": ball,
        "not_ball": not_ball,
        "unclear": unclear,
        "total": total,
        "precision": round(ball / max(ball + not_ball, 1), 3),
    }


@app.get("/sw.js")
async def service_worker():
    """Serve SW from root so it can control all pages."""
    sw_path = Path(__file__).parent / "static" / "sw.js"
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/")
async def root_redirect():
    """Redirect root to the annotation UI."""
    return RedirectResponse(url="/static/annotate.html")


# --- Gap Review endpoints (trajectory gaps for human review) ---


@app.get("/api/gap-reviews")
def get_gap_reviews():
    """List all pending gap review packets."""
    packets = []
    if not REVIEW_PACKETS_DIR.exists():
        return packets

    for packet_dir in sorted(REVIEW_PACKETS_DIR.iterdir()):
        if not packet_dir.is_dir() or packet_dir.name.startswith("_"):
            continue
        manifest_path = packet_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        with open(manifest_path) as f:
            m = json.load(f)
        # Include packets with gap items OR confirm_game_ball type
        gap_items = [
            i
            for i in m.get("items", [])
            if i.get("reason", "").startswith("trajectory_gap")
        ]
        review_type = m.get("review_type", "gap_review")
        if not gap_items and review_type != "confirm_game_ball":
            continue

        results_path = packet_dir / "annotation_results.json"
        reviewed = 0
        if results_path.exists():
            with open(results_path) as f:
                reviewed = len(json.load(f))

        review_type = m.get("review_type", "gap_review")
        all_items = m.get("items", [])
        total = len(all_items)
        packets.append(
            {
                "packet_id": packet_dir.name,
                "game_id": m.get("game_id", ""),
                "review_type": review_type,
                "total_gaps": total,
                "item_count": total,
                "tile_count": m.get("tile_count", 0),
                "reviewed": reviewed,
                "remaining": total - reviewed,
                "created_at": m.get("created_at", 0),
            }
        )

    packets.sort(key=lambda p: p["remaining"], reverse=True)
    return packets


@app.get("/api/gap-reviews/{packet_id}")
def get_gap_review_detail(packet_id: str):
    """Get full gap review packet with items and review status."""
    packet_dir = REVIEW_PACKETS_DIR / packet_id
    manifest_path = packet_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "Packet not found")

    with open(manifest_path) as f:
        m = json.load(f)

    # Load existing results
    results_path = packet_dir / "annotation_results.json"
    results = []
    if results_path.exists():
        with open(results_path) as f:
            results = json.load(f)
    reviewed_stems = {r.get("tile_stem") for r in results}

    # All reviewable items (gap reviews + track confirmations)
    all_items = m.get("items", [])
    for item in all_items:
        stem = item.get("tile_stem", "")
        item["has_image"] = (packet_dir / f"{stem}.jpg").exists()
        item["reviewed"] = stem in reviewed_stems

    unreviewed = [i for i in all_items if not i.get("reviewed")]

    return {
        **m,
        "packet_id": packet_id,
        "items": all_items,
        "reviewed_count": len(reviewed_stems),
        "remaining_count": len(unreviewed),
        "next_item": unreviewed[0] if unreviewed else None,
    }


@app.get("/api/gap-reviews/{packet_id}/tile/{tile_stem:path}")
def get_gap_tile(packet_id: str, tile_stem: str):
    """Serve a tile image from a gap review packet.

    First checks for a pre-extracted JPG. If not found, reads the tile
    on demand from the game's manifest + pack files.
    """
    _img_cache = {"Cache-Control": "public, max-age=604800, immutable"}
    tile_path = REVIEW_PACKETS_DIR / packet_id / f"{tile_stem}.jpg"
    if tile_path.exists():
        return FileResponse(tile_path, media_type="image/jpeg", headers=_img_cache)

    # Read on demand from pack
    manifest_path = REVIEW_PACKETS_DIR / packet_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "Packet not found")

    with open(manifest_path) as f:
        pkt = json.load(f)
    game_id = pkt.get("game_id", "")

    game_dir = Path("D:/training_data/games") / game_id
    if not (game_dir / "manifest.db").exists():
        raise HTTPException(404, "Game manifest not found")

    from training.data_prep.game_manifest import GameManifest
    from training.tasks.io import TaskIO
    from training.tasks.sonnet_qa import _read_tile_from_packs

    manifest = GameManifest(game_dir)
    manifest.open(create=False)
    try:
        io = TaskIO(game_id, Path("G:/pipeline_work"), "")
        packs_dir = io.server_packs()
        jpeg_bytes = _read_tile_from_packs(manifest, tile_stem, packs_dir)
    finally:
        manifest.close()

    if not jpeg_bytes:
        raise HTTPException(404, "Tile not found in packs")

    # Cache for next time
    tile_path.write_bytes(jpeg_bytes)
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/api/gap-reviews/{packet_id}/filmstrip/{tile_stem:path}")
def get_gap_filmstrip(packet_id: str, tile_stem: str):
    """Serve the filmstrip composite for a gap tile."""
    filmstrip_path = (
        REVIEW_PACKETS_DIR / packet_id / "filmstrips" / f"{tile_stem}_filmstrip.jpg"
    )
    if not filmstrip_path.exists():
        raise HTTPException(404, "Filmstrip not found")
    return FileResponse(
        filmstrip_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.post("/api/gap-reviews/{packet_id}/result")
def submit_gap_result(packet_id: str, result: dict):
    """Submit a gap review result.

    Expected: {
        "tile_stem": str,
        "action": "locate"|"out_of_play"|"obscured"|"cant_tell",
        "ball_position": {"x": int, "y": int} | null,  (for "locate")
    }
    """
    packet_dir = REVIEW_PACKETS_DIR / packet_id
    results_path = packet_dir / "annotation_results.json"

    results = []
    if results_path.exists():
        with open(results_path) as f:
            results = json.load(f)

    # Replace existing result for same tile_stem
    tile_stem = result.get("tile_stem", "")
    results = [r for r in results if r.get("tile_stem") != tile_stem]
    results.append(
        {
            "tile_stem": tile_stem,
            "action": result.get("action", ""),
            "ball_position": result.get("ball_position"),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    # Return updated status
    manifest_path = packet_dir / "manifest.json"
    total_gaps = 0
    if manifest_path.exists():
        with open(manifest_path) as f:
            m = json.load(f)
        total_gaps = sum(
            1
            for i in m.get("items", [])
            if i.get("reason", "").startswith("trajectory_gap")
        )

    return {
        "accepted": True,
        "reviewed": len(results),
        "remaining": total_gaps - len(results),
    }


# ------------------------------------------------------------------
# Game Phases API
# ------------------------------------------------------------------

GAMES_DIR = Path("D:/training_data/games")


@app.get("/api/game-phases")
async def list_game_phases():
    """List all games with their detected phases.

    Only includes games that are at least TILED in the pipeline
    (excludes REGISTERED, STAGING, EXCLUDED).
    """
    from training.data_prep.game_manifest import GameManifest

    # Get pipeline states to filter out incomplete games
    tiled_states = {"TILED", "LABELED", "QA_DONE", "TRAINABLE"}
    pipeline_states = {}
    try:
        from training.pipeline.client import PipelineClient

        client = PipelineClient()
        for g in client.get_all_games():
            pipeline_states[g["game_id"]] = g.get("pipeline_state", "")
    except Exception:
        pass  # If pipeline API unavailable, show all games

    results = []
    if not GAMES_DIR.exists():
        return results

    for game_dir in sorted(GAMES_DIR.iterdir()):
        if not game_dir.is_dir():
            continue
        manifest_path = game_dir / "manifest.db"
        if not manifest_path.exists():
            continue

        # Skip games not fully tiled (if we have pipeline state info)
        if (
            pipeline_states
            and pipeline_states.get(game_dir.name, "") not in tiled_states
        ):
            continue

        try:
            gm = GameManifest(game_dir)
            gm.open(create=False)
            phases = gm.get_phases()
            summary = gm.get_metadata("game_phases_summary")
            segments = gm.get_segment_summary()
            gm.close()

            results.append(
                {
                    "game_id": game_dir.name,
                    "has_phases": len(phases) > 0,
                    "phases": [
                        {
                            "phase": p["phase"],
                            "segment_start": p["segment_start"],
                            "frame_start": p["frame_start"],
                            "segment_end": p["segment_end"],
                            "frame_end": p["frame_end"],
                            "source": p["source"],
                            "confidence": p.get("confidence"),
                            "confirmed_by": p.get("confirmed_by"),
                        }
                        for p in phases
                    ],
                    "segments": [
                        {
                            "segment": s["segment"],
                            "frame_min": s["frame_min"],
                            "frame_max": s["frame_max"],
                            "frame_count": s["frame_count"],
                        }
                        for s in segments
                    ],
                    "summary": json.loads(summary) if summary else None,
                }
            )
        except Exception as e:
            logger.debug("Skipping %s for phases: %s", game_dir.name, e)

    return results


@app.get("/api/game-phases/{game_id}")
async def get_game_phases(game_id: str):
    """Get phase details for a specific game."""
    from training.data_prep.game_manifest import GameManifest
    from training.tasks.phase_detect import parse_segment_time

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)
    phases = gm.get_phases()
    segments = gm.get_segment_summary()
    gm.close()

    # Enrich segments with parsed time info
    enriched_segments = []
    for s in segments:
        start_sec = parse_segment_time(s["segment"])
        enriched_segments.append(
            {
                "segment": s["segment"],
                "frame_min": s["frame_min"],
                "frame_max": s["frame_max"],
                "frame_count": s["frame_count"],
                "start_time_sec": start_sec,
                "start_time_str": _fmt_secs(start_sec) if start_sec else None,
            }
        )

    return {
        "game_id": game_id,
        "phases": [
            {
                "id": p.get("id"),
                "phase": p["phase"],
                "segment_start": p["segment_start"],
                "frame_start": p["frame_start"],
                "segment_end": p["segment_end"],
                "frame_end": p["frame_end"],
                "source": p["source"],
                "confidence": p.get("confidence"),
                "confirmed_by": p.get("confirmed_by"),
            }
            for p in phases
        ],
        "segments": enriched_segments,
    }


@app.post("/api/game-phases/{game_id}")
async def save_game_phases(game_id: str, request: Request):
    """Save human-adjusted phase boundaries.

    Expects JSON body: {"phases": [{"phase": str, "segment_start": str,
    "frame_start": int, "segment_end": str, "frame_end": int}, ...]}
    """
    from training.data_prep.game_manifest import GameManifest

    body = await request.json()
    phases = body.get("phases", [])

    if not phases:
        raise HTTPException(400, "No phases provided")

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)
    gm.replace_phases(phases, source="human")
    gm.close()

    logger.info(
        "Saved %d human-confirmed phases for %s: %s",
        len(phases),
        game_id,
        ", ".join(p["phase"] for p in phases),
    )

    return {"accepted": True, "phase_count": len(phases)}


@app.get("/api/game-phases/{game_id}/thumb/{segment}/{frame_idx}")
async def get_phase_thumbnail(game_id: str, segment: str, frame_idx: int):
    """Serve a center tile thumbnail for a phase boundary."""
    from training.data_prep.game_manifest import GameManifest

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)

    # Try center tile (row=1, col=3), then fallbacks
    jpeg_bytes = None
    for row, col in [(1, 3), (0, 3), (1, 2), (1, 4)]:
        jpeg_bytes = gm.read_tile_from_pack(segment, int(frame_idx), row, col)
        if jpeg_bytes:
            break

    gm.close()

    if not jpeg_bytes:
        raise HTTPException(404, "Tile not found")

    return Response(content=jpeg_bytes, media_type="image/jpeg")


def _fmt_secs(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ------------------------------------------------------------------
# Phase Editor: sample points + panoramic frame serving
# ------------------------------------------------------------------


@app.get("/api/game-phases/{game_id}/samples")
async def get_phase_samples(game_id: str, interval: int = 30):
    """Return evenly-spaced sample frames for the phase editor filmstrip.

    Each sample is a (segment, frame_idx) pair at ~`interval` seconds apart,
    spanning the full game timeline.
    """
    from training.data_prep.game_manifest import GameManifest
    from training.tasks.phase_detect import (
        FPS,
        _build_segment_timeline,
    )

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)

    try:
        segments = gm.get_segments()
        timeline = _build_segment_timeline(segments, gm)
        if not timeline:
            return {
                "game_id": game_id,
                "interval_sec": interval,
                "samples": [],
                "total_duration_sec": 0,
            }

        # Compute total duration from first segment start to last segment end
        first_start = timeline[0]["start_sec"]
        last_end = timeline[-1]["end_sec"]
        total_duration = last_end - first_start

        # Build sample points at regular intervals
        samples = []
        t = 0.0
        idx = 0
        while t <= total_duration:
            abs_time = first_start + t

            # Find the segment containing this absolute time
            best_seg = None
            for seg_info in timeline:
                if seg_info["start_sec"] <= abs_time <= seg_info["end_sec"]:
                    best_seg = seg_info
                    break
            # If between segments (gap), use the nearest segment
            if best_seg is None:
                best_seg = min(
                    timeline,
                    key=lambda s: min(
                        abs(s["start_sec"] - abs_time), abs(s["end_sec"] - abs_time)
                    ),
                )

            # Convert abs time to frame_idx within segment
            offset_in_seg = abs_time - best_seg["start_sec"]
            frame_idx = best_seg["frame_min"] + int(offset_in_seg * FPS)
            # Snap to nearest tiled frame (multiple of 4)
            frame_idx = (frame_idx // 4) * 4
            frame_idx = max(
                best_seg["frame_min"], min(frame_idx, best_seg["frame_max"])
            )

            # Verify this frame has tiles
            tile_check = gm.conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE segment=? AND frame_idx=?",
                (best_seg["segment"], frame_idx),
            ).fetchone()[0]
            if tile_check == 0:
                # Find nearest frame with tiles
                nearest = gm.conn.execute(
                    "SELECT frame_idx FROM tiles WHERE segment=? ORDER BY ABS(frame_idx - ?) LIMIT 1",
                    (best_seg["segment"], frame_idx),
                ).fetchone()
                if nearest:
                    frame_idx = nearest[0]

            samples.append(
                {
                    "index": idx,
                    "segment": best_seg["segment"],
                    "frame_idx": frame_idx,
                    "time_sec": round(t, 1),
                    "time_str": _fmt_secs(t),
                }
            )
            idx += 1
            t += interval

        # Pack restoration is no longer needed here — the frame endpoint
        # reads from phase_samples cache or falls back to F: archive directly.

    finally:
        gm.close()

    return {
        "game_id": game_id,
        "interval_sec": interval,
        "samples": samples,
        "total_duration_sec": round(total_duration, 1),
    }


def _ensure_sample_packs(gm, game_dir: Path, samples: list[dict]):
    """Ensure pack files for sampled frames exist on D:, restoring from F: if needed."""
    import shutil

    try:
        from training.pipeline.config import load_config

        cfg = load_config()
        archive_base = Path(cfg.paths.archive.tile_packs) / game_dir.name
    except Exception:
        return

    if not archive_base.exists():
        return

    packs_dir = game_dir / "tile_packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    # Collect unique pack files needed
    needed = set()
    for sample in samples:
        tiles = gm.conn.execute(
            "SELECT DISTINCT pack_file FROM tiles WHERE segment=? AND frame_idx=?",
            (sample["segment"], sample["frame_idx"]),
        ).fetchall()
        for row in tiles:
            if row[0]:
                needed.add(Path(row[0]).name)

    # Restore missing packs
    for pack_name in needed:
        dest = packs_dir / pack_name
        if not dest.exists():
            src = archive_base / pack_name
            if src.exists():
                logger.info(
                    "Phase editor: restoring %s from F: (%.1f GB)",
                    pack_name,
                    src.stat().st_size / 1e9,
                )
                shutil.copy2(str(src), str(dest))


@app.get("/api/game-phases/{game_id}/frame/{segment}/{frame_idx}")
async def get_phase_frame(game_id: str, segment: str, frame_idx: int):
    """Serve a full panoramic frame stitched from tiles, downscaled for the browser."""
    import cv2
    from training.data_prep.game_manifest import GameManifest
    from training.tasks.field_boundary import reconstruct_panoramic

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    # Check pre-loaded cache on G: SSD first, then D: fallback
    frame_name = f"{segment}_{int(frame_idx):06d}.jpg"
    for cache_base in [
        Path("G:/pipeline_work/phase_samples") / game_id,
        game_dir / "phase_samples",
    ]:
        cache_file = cache_base / frame_name
        if cache_file.exists():
            return Response(
                content=cache_file.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=604800, immutable"},
            )

    gm = GameManifest(game_dir)
    gm.open(create=False)

    # Try D: packs, then F: archive directly (no full pack restore)
    packs_dir = game_dir / "tile_packs"
    pano = reconstruct_panoramic(gm, segment, int(frame_idx), packs_dir)
    gm.close()

    if pano is None:
        raise HTTPException(404, "Could not reconstruct panoramic frame")

    # Flip 180 if this game was recorded upside down
    try:
        from training.pipeline.config import load_config
        from training.pipeline.registry import GameRegistry

        cfg = load_config()
        reg = GameRegistry(cfg.paths.registry_db)
        game = reg.get_game(game_id)
        if game and game.get("needs_flip"):
            pano = cv2.rotate(pano, cv2.ROTATE_180)
        reg.close()
    except Exception:
        pass

    # Downscale to half-res (same as field boundary panoramic endpoint)
    h, w = pano.shape[:2]
    small = cv2.resize(pano, (w // 2, h // 2))
    _, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])

    # Cache on G: SSD for next time
    ssd_cache = Path("G:/pipeline_work/phase_samples") / game_id / frame_name
    ssd_cache.parent.mkdir(parents=True, exist_ok=True)
    ssd_cache.write_bytes(jpeg.tobytes())

    return Response(
        content=jpeg.tobytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


# ------------------------------------------------------------------
# Field Boundary API
# ------------------------------------------------------------------


@app.get("/api/field-boundary")
async def list_field_boundaries():
    """List all games with their field boundary status."""
    from training.data_prep.game_manifest import GameManifest

    results = []
    if not GAMES_DIR.exists():
        return results

    for game_dir in sorted(GAMES_DIR.iterdir()):
        if not game_dir.is_dir():
            continue
        manifest_path = game_dir / "manifest.db"
        if not manifest_path.exists():
            continue

        try:
            gm = GameManifest(game_dir)
            gm.open(create=False)
            segs = gm.get_segments()
            if not segs:
                gm.close()
                continue  # empty manifest (still tiling)
            fb_raw = gm.get_metadata("field_boundary")
            gm.close()

            fb = json.loads(fb_raw) if fb_raw else None
            results.append(
                {
                    "game_id": game_dir.name,
                    "has_polygon": fb is not None and fb.get("polygon") is not None,
                    "source": fb.get("source") if fb else None,
                    "confidence": fb.get("confidence") if fb else None,
                    "needs_human_review": fb.get("needs_human_review", True)
                    if fb
                    else True,
                }
            )
        except Exception as e:
            logger.debug("Skipping %s for field boundary: %s", game_dir.name, e)

    # Find games still tiling (have no manifest yet) via pipeline API
    not_tiled = []
    try:
        import httpx

        pipe_resp = httpx.get("http://127.0.0.1:8643/api/games", timeout=5)
        pipe_games = pipe_resp.json()
        known_ids = {r["game_id"] for r in results}
        for g in pipe_games:
            if g["game_id"] not in known_ids and g.get("pipeline_state") in (
                "REGISTERED",
                "STAGING",
            ):
                not_tiled.append(g["game_id"])
    except Exception:
        pass

    return {"games": results, "not_tiled": not_tiled}


@app.get("/api/field-boundary/{game_id}")
async def get_field_boundary(game_id: str):
    """Get field boundary polygon for a specific game."""
    from training.data_prep.game_manifest import GameManifest

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)
    fb_raw = gm.get_metadata("field_boundary")
    gm.close()

    if not fb_raw:
        # Return a default 10-point template so the editor has something to drag
        default_poly = [
            [100, 1300],
            [800, 1500],
            [1600, 1600],
            [2500, 1600],
            [3400, 1500],
            [3900, 400],
            [3000, 350],
            [2100, 320],
            [1200, 350],
            [200, 400],
        ]
        return {
            "game_id": game_id,
            "polygon": default_poly,
            "source": "template",
            "confidence": 0,
            "needs_human_review": True,
        }

    fb = json.loads(fb_raw)
    fb["game_id"] = game_id
    return fb


@app.post("/api/field-boundary/{game_id}")
async def save_field_boundary(game_id: str, request: Request):
    """Save human-drawn field boundary polygon.

    Expects JSON body: {"polygon": [[x1,y1], [x2,y2], ...]}
    """
    from training.data_prep.game_manifest import GameManifest

    body = await request.json()
    polygon = body.get("polygon")

    if not polygon or len(polygon) < 4:
        raise HTTPException(400, "Polygon must have at least 4 points")

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    fb_result = {
        "polygon": polygon,
        "source": "human",
        "confidence": 1.0,
        "needs_human_review": False,
        "created_at": time.time(),
    }

    gm = GameManifest(game_dir)
    gm.open(create=False)
    gm.set_metadata("field_boundary", json.dumps(fb_result))
    gm.close()

    logger.info("Saved human field boundary for %s: %d points", game_id, len(polygon))
    return {"accepted": True, "point_count": len(polygon)}


@app.get("/api/field-boundary/{game_id}/panoramic")
async def get_field_boundary_panoramic(
    game_id: str, overlay: bool = True, flip: bool = False
):
    """Serve a panoramic JPEG, optionally with the field boundary polygon overlaid."""
    from training.data_prep.game_manifest import GameManifest
    from training.tasks.field_boundary import reconstruct_panoramic

    game_dir = GAMES_DIR / game_id
    if not game_dir.exists():
        raise HTTPException(404, f"Game not found: {game_id}")

    gm = GameManifest(game_dir)
    gm.open(create=False)

    packs_dir = game_dir / "tile_packs"
    pano = None

    # Try to find a frame with tiles available in packs on disk.
    # Start from the middle of the segment list (more likely to be
    # actual game footage, not setup/transport at the start).
    segments = gm.get_segments()
    mid = len(segments) // 2
    segments = segments[mid:] + segments[:mid]
    for seg in segments:
        # Find a frame in this segment that has pack files on D:
        row = gm.conn.execute(
            """SELECT frame_idx FROM tiles
               WHERE segment = ? AND pack_file IS NOT NULL
               GROUP BY frame_idx HAVING COUNT(*) >= 10
               ORDER BY frame_idx LIMIT 1 OFFSET (
                   SELECT COUNT(DISTINCT frame_idx)/2 FROM tiles
                   WHERE segment = ? AND pack_file IS NOT NULL
               )""",
            (seg, seg),
        ).fetchone()
        if not row:
            continue
        fi = row[0]
        pano = reconstruct_panoramic(gm, seg, fi, packs_dir)
        if pano is not None:
            break

    # Draw polygon overlay if one exists
    fb_raw = gm.get_metadata("field_boundary")
    gm.close()

    if pano is None:
        raise HTTPException(404, "Could not reconstruct panoramic")

    if flip:
        import cv2 as _cv2

        pano = _cv2.flip(pano, -1)

    if overlay and fb_raw:
        fb = json.loads(fb_raw)
        if fb.get("polygon"):
            import numpy as np

            pts = np.array(fb["polygon"], dtype=np.int32)
            poly_layer = pano.copy()
            import cv2

            cv2.fillPoly(poly_layer, [pts], (0, 180, 0))
            pano = cv2.addWeighted(pano, 0.7, poly_layer, 0.3, 0)
            cv2.polylines(pano, [pts], True, (0, 255, 0), 3)

    # Downscale to half for web serving
    import cv2

    h, w = pano.shape[:2]
    small = cv2.resize(pano, (w // 2, h // 2))
    _, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])

    return Response(content=jpeg.tobytes(), media_type="image/jpeg")


# Static file mount must come AFTER all API routes (catch-all).
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
