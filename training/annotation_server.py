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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Configurable via environment or CLI args
REVIEW_PACKETS_DIR = Path("review_packets")
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


# Static file mount must come AFTER all API routes (catch-all).
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
