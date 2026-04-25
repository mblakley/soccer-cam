"""Generate review task — present game ball track for human confirmation.

After Sonnet QA identifies the dominant ball trajectory, this task
packages a filmstrip of the track for human confirmation. The human
confirms "yes, that's the game ball" and then the system can fill
in gaps where the detector lost the ball.

Step 1: "Is this the game ball?" (this task)
Step 2: Gap filling with Sonnet + human (future tasks, after confirmation)
"""

import json
import logging
import subprocess
import time
from pathlib import Path

from training.tasks import register_task
from training.tasks.io import TaskIO

logger = logging.getLogger(__name__)


@register_task("generate_review")
def run_generate_review(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Package the dominant ball track for human confirmation."""
    game_id = item["game_id"]

    from training.pipeline.config import load_config

    cfg = load_config()

    # Pull manifest to local SSD
    task_io = TaskIO(game_id, local_work_dir, server_share)
    task_io.ensure_space(needed_gb=3)
    task_io.pull_manifest()

    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(task_io.local_game)
    manifest.open(create=False)

    try:
        # Read track info saved by sonnet_qa Phase 2
        track_info_raw = manifest.conn.execute(
            "SELECT value FROM metadata WHERE key = 'game_ball_track'"
        ).fetchone()

        if not track_info_raw:
            logger.info("No game ball track found for %s — nothing to review", game_id)
            return {"review_count": 0}

        track_info = json.loads(track_info_raw[0])
        track_points_raw = manifest.conn.execute(
            "SELECT value FROM metadata WHERE key = 'game_ball_track_points'"
        ).fetchone()
        track_points = json.loads(track_points_raw[0]) if track_points_raw else []

        # Resolve pack location (read directly, no copy — only need a few tiles)
        packs_dir = task_io.server_packs()

        # Build review packet
        review_dir = (
            Path(cfg.paths.games_dir).parent
            / "review_packets"
            / f"{game_id}_{int(time.time())}"
        )
        review_dir.mkdir(parents=True, exist_ok=True)

        # Build filmstrip of the dominant track (evenly spaced samples)
        from training.tasks.sonnet_qa import _get_trajectory_sample_frames
        from training.data_prep.trajectory_gaps import build_gap_filmstrip

        traj_tuples = [(p["fi"], p["seg"], p["px"], p["py"]) for p in track_points]
        sample_frames = _get_trajectory_sample_frames(
            traj_tuples,
            n_samples=8,
            manifest=manifest,
        )

        filmstrip_path = review_dir / "game_ball_track.jpg"
        if sample_frames:
            build_gap_filmstrip(sample_frames, manifest, packs_dir, filmstrip_path)

        # Also extract individual tile images for the track samples
        from training.tasks.sonnet_qa import _read_tile_from_packs

        tile_images = []
        for frame in sample_frames:
            tile_stem = frame["tile_stem"]
            jpeg_bytes = _read_tile_from_packs(manifest, tile_stem, packs_dir)
            if jpeg_bytes:
                tile_path = review_dir / f"{tile_stem}.jpg"
                tile_path.write_bytes(jpeg_bytes)
                tile_images.append(tile_stem)

        # Write review manifest
        # Tag each frame with its game phase (for display in review UI)
        items = []
        for frame in sample_frames:
            phase = manifest.get_phase_for_frame(frame["segment"], frame["frame_idx"])
            items.append(
                {
                    "tile_stem": frame["tile_stem"],
                    "frame_idx": frame["frame_idx"],
                    "reason": "confirm_game_ball",
                    "priority": 300,
                    "role": "track_sample",
                    "phase": phase,
                }
            )

        # Get phase summary for the review manifest
        phases = manifest.get_phases()
        phase_summary = (
            [{"phase": p["phase"], "source": p["source"]} for p in phases]
            if phases
            else None
        )

        review_manifest = {
            "game_id": game_id,
            "created_at": time.time(),
            "review_type": "confirm_game_ball",
            "track_info": track_info,
            "filmstrip": "game_ball_track.jpg",
            "tile_count": len(tile_images),
            "game_phases": phase_summary,
            "items": items,
        }
        (review_dir / "manifest.json").write_text(json.dumps(review_manifest, indent=2))

        # Pre-extract phase editor sample frames so human review is instant
        _preload_phase_samples(manifest, task_io, cfg)
    finally:
        manifest.close()

    # Send NTFY
    if cfg.ntfy.enabled:
        track_frames = track_info.get("track_frames", 0)
        seg_frames = track_info.get("segment_frames", 0)
        coverage = round(track_frames / max(seg_frames, 1) * 100)
        try:
            subprocess.run(
                [
                    "curl",
                    "-s",
                    "-H",
                    "Title: Confirm Game Ball Track",
                    "-H",
                    "Priority: default",
                    "-H",
                    f"Tags: soccer,{game_id}",
                    "-d",
                    f"Is this the game ball?\n"
                    f"Game: {game_id}\n"
                    f"Track: {track_frames} frames ({coverage}% of segment)\n"
                    f"Review: https://trainer.goat-rattlesnake.ts.net/static/annotate.html#gap-review",
                    f"https://ntfy.sh/{cfg.ntfy.topic}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)

    logger.info(
        "Generated track confirmation packet for %s: %d sample tiles, "
        "track=%d frames (%d-%d), segment=%d frames",
        game_id,
        len(tile_images),
        track_info["track_frames"],
        track_info["track_start"],
        track_info["track_end"],
        track_info["segment_frames"],
    )

    return {
        "review_count": len(tile_images),
        "review_dir": str(review_dir),
        "review_type": "confirm_game_ball",
        "track_coverage_pct": coverage,
    }


def _preload_phase_samples(manifest, task_io, cfg, interval: int = 30):
    """Pre-extract panoramic frames for the phase editor.

    Reads tiles directly from pack files (local or F: archive) and stitches
    them into downscaled panoramic JPEGs cached at:
        D:/training_data/games/{game_id}/phase_samples/{segment}_{frame_idx}.jpg

    This avoids copying entire multi-GB pack files to D: just for phase review.
    """
    import cv2

    from training.tasks.field_boundary import reconstruct_panoramic
    from training.tasks.phase_detect import FPS, _build_segment_timeline

    game_id = manifest.game_id
    cache_dir = Path(cfg.paths.games_dir) / game_id / "phase_samples"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Check if this game needs 180-degree rotation
    needs_flip = False
    try:
        from training.pipeline.registry import GameRegistry

        reg = GameRegistry(cfg.paths.registry_db)
        game_info = reg.get_game(game_id)
        if game_info and game_info.get("needs_flip"):
            needs_flip = True
        reg.close()
    except Exception:
        pass

    segments = manifest.get_segments()
    timeline = _build_segment_timeline(segments, manifest)
    if not timeline:
        logger.info("No timeline for %s, skipping phase sample preload", game_id)
        return

    first_start = timeline[0]["start_sec"]
    last_end = timeline[-1]["end_sec"]
    total_duration = last_end - first_start

    # Determine where to read packs from — prefer local, fallback to F:
    packs_dir = Path(cfg.paths.games_dir) / game_id / "tile_packs"
    if not packs_dir.exists() or not any(packs_dir.glob("*.pack")):
        # Try F: archive
        f_packs = Path(cfg.paths.archive.tile_packs) / game_id
        if f_packs.exists():
            packs_dir = f_packs
        else:
            logger.warning("No packs found for %s, skipping preload", game_id)
            return

    # Build sample points at regular intervals (same logic as annotation_server)
    samples_created = 0
    t = 0.0
    while t <= total_duration:
        abs_time = first_start + t

        best_seg = None
        for seg_info in timeline:
            if seg_info["start_sec"] <= abs_time <= seg_info["end_sec"]:
                best_seg = seg_info
                break
        if best_seg is None:
            best_seg = min(
                timeline,
                key=lambda s: min(
                    abs(s["start_sec"] - abs_time), abs(s["end_sec"] - abs_time)
                ),
            )

        offset_in_seg = abs_time - best_seg["start_sec"]
        frame_idx = best_seg["frame_min"] + int(offset_in_seg * FPS)
        frame_idx = (frame_idx // 4) * 4
        frame_idx = max(best_seg["frame_min"], min(frame_idx, best_seg["frame_max"]))

        # Snap to nearest tiled frame
        tile_check = manifest.conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE segment=? AND frame_idx=?",
            (best_seg["segment"], frame_idx),
        ).fetchone()[0]
        if tile_check == 0:
            nearest = manifest.conn.execute(
                "SELECT frame_idx FROM tiles WHERE segment=? ORDER BY ABS(frame_idx - ?) LIMIT 1",
                (best_seg["segment"], frame_idx),
            ).fetchone()
            if nearest:
                frame_idx = nearest[0]

        cache_file = cache_dir / f"{best_seg['segment']}_{frame_idx:06d}.jpg"
        if not cache_file.exists():
            pano = reconstruct_panoramic(
                manifest, best_seg["segment"], frame_idx, packs_dir
            )
            if pano is not None:
                if needs_flip:
                    pano = cv2.rotate(pano, cv2.ROTATE_180)
                h, w = pano.shape[:2]
                small = cv2.resize(pano, (w // 2, h // 2))
                cv2.imwrite(str(cache_file), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                samples_created += 1

        t += interval

    logger.info(
        "Pre-loaded %d phase sample frames for %s in %s",
        samples_created,
        game_id,
        cache_dir,
    )
