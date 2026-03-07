"""FastAPI annotation server for mobile review over Tailscale.

Runs on the training PC, serves review packet crops and collects
annotation results from the Flutter app. No auth needed -- Tailscale
provides the private network boundary.

Usage:
    uvicorn training.annotation_server:app --host 0.0.0.0 --port 8642
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Configurable via environment or CLI args
REVIEW_PACKETS_DIR = Path("review_packets")
LABELS_OUTPUT_DIR = Path("training_data/labels/annotations")

app = FastAPI(title="Ball Tracking Annotation Server", version="0.1.0")


# --- Request/Response models ---


class AnnotationResult(BaseModel):
    frame_idx: int
    action: str  # confirm, reject, adjust, locate, not_visible, skip
    ball_position: dict | None = None  # {"x": int, "y": int}
    duration_ms: int | None = None


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
        existing_by_frame[result.frame_idx] = {
            "frame_idx": result.frame_idx,
            "action": result.action,
            "ball_position": result.ball_position,
            "duration_ms": result.duration_ms,
            "reviewer": "phone",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }

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


@app.post("/api/ingest")
async def trigger_ingestion():
    """Trigger ingestion of completed annotation results into YOLO labels.

    Only processes packets with annotation_results.json that haven't been
    ingested yet.
    """
    from training.correction_ingester import ingest_all_packets

    stats_list = ingest_all_packets(REVIEW_PACKETS_DIR, LABELS_OUTPUT_DIR)

    return JSONResponse(
        {
            "packets_processed": len(stats_list),
            "total_labels_written": sum(s.labels_written for s in stats_list),
        }
    )
