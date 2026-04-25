"""Sonnet QA task — verify ball detections using Claude vision via CLI.

Runs on the server. Builds composite grid images from tiles, sends them
to Claude for BALL/NOT_BALL classification, writes qa_verdict to manifest.

Pull-local-process-push pattern:
  - Pull: copy pack files + manifest.db to local SSD
  - Process: extract uncertain tiles, build grids, call claude CLI
  - Push: copy updated manifest.db back to server
  - Rate-limited: ~100 batches/hr max

Usage (as task): enqueued by orchestrator for LABELED games
"""

import json
import logging
import subprocess
import time
from pathlib import Path

from training.tasks import register_task
from training.tasks.io import TaskIO

logger = logging.getLogger(__name__)

# Grid layout for composite images
GRID_COLS = 3
GRID_ROWS = 2
TILES_PER_GRID = GRID_COLS * GRID_ROWS  # 6 tiles per image


@register_task("sonnet_qa")
def run_sonnet_qa(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Run Sonnet vision QA on uncertain detections for a game."""
    game_id = item["game_id"]

    from training.pipeline.config import load_config

    cfg = load_config()

    # Step 1: Pull manifest to local SSD (lightweight — ~200MB)
    task_io = TaskIO(game_id, local_work_dir, server_share)
    task_io.ensure_space(needed_gb=3)
    task_io.pull_manifest()

    # Step 2: Get tiles that need QA (before pulling packs)
    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(task_io.local_game)
    manifest.open(create=False)

    try:
        return _run_qa(manifest, task_io, cfg, game_id)
    finally:
        manifest.close()


def _run_qa(manifest, task_io, cfg, game_id: str) -> dict:
    """Inner QA logic — separated so manifest.close() is guaranteed by caller."""
    # Phase detection is now a separate task (phase_detect) — skip here.
    # Phases should already exist from a prior phase_detect run.

    # Phase 0.5: Detect field boundary polygon if not yet done
    _ensure_field_boundary(manifest, task_io)

    candidates = _get_qa_candidates(
        manifest, max_tiles=cfg.qa.sonnet_batch_limit * cfg.qa.sonnet_batch_size
    )

    if not candidates:
        # Check if Phase 2 (trajectory) still needs to run
        existing_track = manifest.conn.execute(
            "SELECT value FROM metadata WHERE key = 'game_ball_track'"
        ).fetchone()
        if existing_track:
            logger.info("No QA candidates for %s", game_id)
            return {"tiles_reviewed": 0, "verdicts": {}}
        logger.info(
            "No QA candidates for %s — but game_ball_track missing, running Phase 2",
            game_id,
        )
        candidates = []  # fall through to Phase 2

    packs_dir = task_io.local_packs

    if candidates:
        logger.info("QA: %d candidate tiles for %s", len(candidates), game_id)

        # Step 3: Pull only the pack files that QA candidates reference to SSD
        needed_packs = _find_needed_packs(candidates, manifest)
        _pull_selective_packs(task_io, needed_packs)

    # Step 4: Process in batches
    batch_size = cfg.qa.sonnet_batch_size
    max_batches = cfg.qa.sonnet_batch_limit
    total_reviewed = 0
    verdicts = {"ball": 0, "not_ball": 0, "error": 0}

    grid_num = 0
    for batch_idx in range(0, len(candidates), batch_size):
        if batch_idx // batch_size >= max_batches:
            logger.info("Rate limit reached (%d batches), stopping", max_batches)
            break

        batch = candidates[batch_idx : batch_idx + batch_size]

        # Build composite grids (save to local work dir for claude to read)
        grids = _build_grids(batch, manifest, packs_dir, task_io.local_game)

        for grid_info in grids:
            grid_num += 1
            n_tiles = len(grid_info["tile_stems"])
            logger.info(
                "Grid %d/%d: calling Claude on %d tiles (%s)...",
                grid_num,
                max_batches,
                n_tiles,
                grid_info["image_path"].name,
            )
            try:
                t0 = time.time()
                results = _call_claude(grid_info["image_path"], grid_info["tile_stems"])
                elapsed = time.time() - t0

                balls = 0
                for stem, verdict in results.items():
                    if verdict in ("BALL", "TRUE_POSITIVE"):
                        manifest.set_qa_verdict(stem, "true_positive")
                        verdicts["ball"] += 1
                        balls += 1
                    elif verdict in ("NOT_BALL", "FALSE_POSITIVE"):
                        manifest.set_qa_verdict(stem, "false_positive")
                        verdicts["not_ball"] += 1
                    else:
                        verdicts["error"] += 1
                    total_reviewed += 1

                logger.info(
                    "Grid %d: %d/%d BALL in %.1fs (total: %d reviewed, %d ball, %d not_ball)",
                    grid_num,
                    balls,
                    n_tiles,
                    elapsed,
                    total_reviewed,
                    verdicts["ball"],
                    verdicts["not_ball"],
                )

            except Exception as e:
                logger.exception("Claude QA grid %d failed: %s", grid_num, e)
                verdicts["error"] += n_tiles

            # Brief pause between API calls
            time.sleep(2)

    # ================================================================
    # Phase 2: Trajectory gap detection — find where ball disappeared
    # ================================================================
    gap_verdicts = {"found": 0, "not_found": 0, "error": 0}
    trajectories = []  # hoisted for Phase 3

    try:
        from training.data_prep.trajectory_gaps import (
            build_trajectories_from_manifest,
            stitch_game_ball_track,
            find_gap_candidates,
            gap_to_tile_stem,
            build_gap_filmstrip,
        )

        has_phases = bool(manifest.get_phases())
        raw_trajectories = build_trajectories_from_manifest(
            manifest.conn,
            min_length=cfg.qa.min_trajectory_length,
            play_phases_only=has_phases,
        )

        # Stitch fragments into continuous game ball tracks
        trajectories = stitch_game_ball_track(
            raw_trajectories,
            max_gap_seconds=3.0,
            frame_interval=cfg.tiling.frame_interval,
        )

        if trajectories:
            # Pick the longest track — most likely game ball
            dominant = trajectories[0]
            dom_disp = max(
                ((p[2] - dominant[0][2]) ** 2 + (p[3] - dominant[0][3]) ** 2) ** 0.5
                for p in dominant[1:]
            )

            # Get segment frame range to compare track extent vs video
            segment = dominant[0][1]
            seg_info = manifest.conn.execute(
                "SELECT frame_min, frame_max, frame_count FROM segments WHERE segment = ?",
                (segment,),
            ).fetchone()
            seg_start = seg_info[0] if seg_info else 0
            seg_end = seg_info[1] if seg_info else 0
            seg_frames = seg_info[2] if seg_info else 0

            track_start = dominant[0][0]
            track_end = dominant[-1][0]

            logger.info(
                "Phase 2: dominant track in %s — %d frames, %.0fpx displacement, "
                "track fi=%d-%d, segment fi=%d-%d (%d frames)",
                segment[:30],
                len(dominant),
                dom_disp,
                track_start,
                track_end,
                seg_start,
                seg_end,
                seg_frames,
            )

            # Pull packs for filmstrip building
            traj_pack_files = set()
            rows_q = manifest.conn.execute(
                "SELECT DISTINCT pack_file FROM tiles WHERE segment = ? AND pack_file IS NOT NULL",
                (segment,),
            ).fetchall()
            for r in rows_q:
                traj_pack_files.add(r[0])
            _pull_selective_packs(task_io, traj_pack_files)

            # Build a filmstrip of the dominant track for human confirmation
            verify_frames = _get_trajectory_sample_frames(dominant, n_samples=6)
            if verify_frames:
                verify_path = task_io.local_game / "game_ball_candidate.jpg"
                build_gap_filmstrip(verify_frames, manifest, packs_dir, verify_path)

            # Store track info for generate_review to package for human
            track_info = {
                "segment": segment,
                "track_start": track_start,
                "track_end": track_end,
                "track_frames": len(dominant),
                "displacement_px": round(dom_disp),
                "segment_start": seg_start,
                "segment_end": seg_end,
                "segment_frames": seg_frames,
                "top_5_tracks": [
                    {
                        "length": len(t),
                        "segment": t[0][1],
                        "start_fi": t[0][0],
                        "end_fi": t[-1][0],
                    }
                    for t in trajectories[:5]
                ],
            }
            manifest.set_metadata("game_ball_track", json.dumps(track_info))

            # Store the track points for later gap filling
            track_points = [
                {"fi": fi, "seg": seg, "px": round(px, 1), "py": round(py, 1)}
                for fi, seg, px, py in dominant
            ]
            manifest.set_metadata("game_ball_track_points", json.dumps(track_points))

            logger.info(
                "Phase 2 complete: dominant track stored (%d frames).",
                len(dominant),
            )
        else:
            logger.info("Phase 2: no moving trajectories found (rows 0-1, >200px disp)")

    except Exception as e:
        logger.exception("Phase 2 failed: %s", e)

    # ================================================================
    # Phase 3: Track-end discovery — check tiles where detector missed
    # ================================================================
    track_end_verdicts = {"found": 0, "not_found": 0, "error": 0}

    try:
        if trajectories:
            gap_candidates = find_gap_candidates(
                trajectories,
                frame_interval=cfg.tiling.frame_interval,
            )
            # Focus on track ends and short mid-gaps
            gap_candidates = [
                g for g in gap_candidates if g["gap_type"] in ("track_end", "mid_gap")
            ]

            # Convert gap positions to tile_stems and filter already-labeled
            labeled_stems = manifest.get_labeled_stems()
            # Also skip tiles already checked in previous QA runs
            qa_checked = set()
            for row in manifest.conn.execute(
                "SELECT tile_stem FROM labels WHERE source = 'sonnet_qa_discovery'"
            ).fetchall():
                qa_checked.add(row[0])

            discovery_stems = []
            for gap in gap_candidates:
                stem = gap_to_tile_stem(
                    gap["segment"], gap["frame_idx"], gap["pano_x"], gap["pano_y"]
                )
                if stem and stem not in labeled_stems and stem not in qa_checked:
                    # Verify tile exists in manifest (frame was actually tiled)
                    tile = manifest.get_tile_by_stem(stem)
                    if tile:
                        discovery_stems.append(
                            {
                                "tile_stem": stem,
                                "gap": gap,
                            }
                        )

            # Limit to a reasonable batch per cycle
            max_discovery = min(
                cfg.qa.sonnet_batch_limit * TILES_PER_GRID,
                len(discovery_stems),
            )
            discovery_stems = discovery_stems[:max_discovery]

            if discovery_stems:
                logger.info(
                    "Phase 3: %d track-end/gap tiles to check for %s",
                    len(discovery_stems),
                    game_id,
                )

                # Ensure packs are pulled for these segments
                disc_packs = _find_needed_packs(discovery_stems, manifest)
                _pull_selective_packs(task_io, disc_packs)

                # Build grids and call Claude
                disc_grids = _build_grids(
                    discovery_stems, manifest, packs_dir, task_io.local_game
                )
                for grid_info in disc_grids:
                    n_tiles = len(grid_info["tile_stems"])
                    logger.info(
                        "Phase 3 grid: calling Claude on %d tiles (%s)...",
                        n_tiles,
                        grid_info["image_path"].name,
                    )
                    try:
                        results = _call_claude(
                            grid_info["image_path"], grid_info["tile_stems"]
                        )
                        for stem, verdict in results.items():
                            if verdict in ("BALL", "TRUE_POSITIVE"):
                                # Find the gap info to get ball position
                                gap_info = next(
                                    (
                                        d["gap"]
                                        for d in discovery_stems
                                        if d["tile_stem"] == stem
                                    ),
                                    None,
                                )
                                if gap_info:
                                    from training.data_prep.trajectory_gaps import (
                                        _pano_to_tile,
                                    )

                                    tile_pos = _pano_to_tile(
                                        gap_info["pano_x"], gap_info["pano_y"]
                                    )
                                    if tile_pos:
                                        _, _, cx, cy = tile_pos
                                        # Create label — extends the track
                                        manifest.upsert_label(
                                            tile_stem=stem,
                                            class_id=0,
                                            cx=cx,
                                            cy=cy,
                                            w=0.03,  # ~20px in 640
                                            h=0.03,
                                            source="sonnet_qa_discovery",
                                            confidence=0.7,
                                        )
                                        manifest.set_qa_verdict(stem, "true_positive")
                                        track_end_verdicts["found"] += 1
                                        logger.info(
                                            "Phase 3: ball found at %s (%.1f, %.1f)",
                                            stem,
                                            gap_info["pano_x"],
                                            gap_info["pano_y"],
                                        )
                                else:
                                    track_end_verdicts["found"] += 1
                            else:
                                # Record that we checked this tile (no ball)
                                # Use a dummy label so we don't re-check
                                manifest.upsert_label(
                                    tile_stem=stem,
                                    class_id=0,
                                    cx=0.5,
                                    cy=0.5,
                                    w=0.01,
                                    h=0.01,
                                    source="sonnet_qa_discovery",
                                    confidence=0.0,
                                )
                                manifest.set_qa_verdict(stem, "false_positive")
                                track_end_verdicts["not_found"] += 1
                        manifest.conn.commit()
                    except Exception as e:
                        logger.exception("Phase 3 grid failed: %s", e)
                        track_end_verdicts["error"] += n_tiles
                    time.sleep(2)

                logger.info(
                    "Phase 3 complete: %d found, %d not found, %d errors",
                    track_end_verdicts["found"],
                    track_end_verdicts["not_found"],
                    track_end_verdicts["error"],
                )
            else:
                logger.info("Phase 3: no unchecked track-end tiles for %s", game_id)

    except Exception as e:
        logger.exception("Phase 3 failed: %s", e)

    # Step 5b: Compute track coverage (reuses trajectories already built)
    coverage_result = {"coverage": 0.0, "gap_count": 0}
    try:
        from training.flywheel.coverage import measure_game_coverage_from_manifest

        coverage_result = measure_game_coverage_from_manifest(manifest.conn)
        manifest.set_metadata("track_coverage", json.dumps(coverage_result))
        logger.info(
            "Track coverage for %s: %.1f%% (%d gaps)",
            game_id,
            coverage_result["coverage"] * 100,
            coverage_result["gap_count"],
        )
    except Exception as e:
        logger.exception("Coverage computation failed: %s", e)

    manifest.set_metadata("qa_at", str(time.time()))

    # Count how many labels still have no QA verdict (for orchestrator exhaustion check)
    unreviewed_remaining = manifest.conn.execute(
        "SELECT COUNT(DISTINCT tile_stem) FROM labels WHERE qa_verdict IS NULL"
    ).fetchone()[0]

    # Close before push to flush WAL — track metadata must be in the
    # main DB file before copying to D:
    manifest.close()
    task_io.push_manifest()

    total_discovery = (
        track_end_verdicts["found"]
        + track_end_verdicts["not_found"]
        + track_end_verdicts["error"]
    )
    logger.info(
        "QA complete for %s: Phase1=%d tiles (ball=%d, not_ball=%d), "
        "Phase3=%d discovery (found=%d, not_found=%d), %d unreviewed remaining",
        game_id,
        total_reviewed,
        verdicts["ball"],
        verdicts["not_ball"],
        total_discovery,
        track_end_verdicts["found"],
        track_end_verdicts["not_found"],
        unreviewed_remaining,
    )

    return {
        "tiles_reviewed": total_reviewed + total_discovery,
        "unreviewed_remaining": unreviewed_remaining,
        "verdicts": verdicts,
        "gap_verdicts": gap_verdicts,
        "track_end_verdicts": track_end_verdicts,
        "track_coverage": coverage_result.get("coverage", 0.0),
        "gap_count": coverage_result.get("gap_count", 0),
    }


def _find_needed_packs(candidates: list[dict], manifest) -> set[str]:
    """Determine which pack files contain tiles we need to QA."""
    import re

    segments = set()
    for cand in candidates:
        m = re.match(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$", cand["tile_stem"])
        if m:
            segments.add(m.group(1))

    # Query manifest for the pack files these segments reference
    conn = manifest.conn
    pack_files = set()
    for segment in segments:
        rows = conn.execute(
            "SELECT DISTINCT pack_file FROM tiles WHERE segment = ? AND pack_file IS NOT NULL",
            (segment,),
        ).fetchall()
        for r in rows:
            pack_files.add(r[0])

    return pack_files


def _pull_selective_packs(task_io: TaskIO, pack_files: set[str]):
    """Copy only the specific pack files needed for QA."""
    import shutil

    task_io.local_packs.mkdir(parents=True, exist_ok=True)
    # Only restore the specific packs we need from F: archive, not all packs
    pack_names = {Path(p).name for p in pack_files}
    server_packs = task_io.ensure_server_packs(pack_names)
    copied = 0
    for pack_path_str in pack_files:
        pack_name = Path(pack_path_str).name
        src = server_packs / pack_name
        dest = task_io.local_packs / pack_name
        src_size = src.stat().st_size if src.exists() else 0
        if dest.exists() and dest.stat().st_size == src_size:
            logger.info(
                "Pack %s already on SSD (%.1f GB), skipping copy",
                pack_name,
                src_size / (1024**3),
            )
        elif src.exists():
            size_gb = src_size / (1024**3)
            if dest.exists():
                logger.info("Pack %s on SSD is wrong size, re-copying...", pack_name)
                dest.unlink()
            logger.info("Copying %s (%.1f GB) to SSD...", pack_name, size_gb)
            shutil.copy2(str(src), str(dest))
            copied += 1
            logger.info("Copied %s (%.1f GB)", pack_name, size_gb)
        else:
            logger.warning("Pack source not found: %s", src)
    logger.info("Pulled %d/%d needed pack files to SSD", copied, len(pack_files))


def _ensure_field_boundary(manifest, task_io: TaskIO):
    """Run field boundary detection if not yet done."""
    existing = manifest.get_metadata("field_boundary")
    if existing:
        return  # already done

    try:
        from training.tasks.field_boundary import detect_field_boundary

        result = detect_field_boundary(manifest, task_io)
        if result:
            source = result.get("source", "unknown")
            confidence = result.get("confidence", 0)
            logger.info(
                "Field boundary: source=%s confidence=%.2f for %s",
                source,
                confidence,
                manifest.game_id,
            )
    except Exception as e:
        logger.warning(
            "Field boundary detection failed for %s: %s", manifest.game_id, e
        )


def _get_qa_candidates(manifest, max_tiles: int = 2000) -> list[dict]:
    """Get tiles that need QA — uncertain detections plus random high-conf sample.

    Always reserves ~20% of the batch for random high-confidence detections
    to verify that ONNX tracks are actually finding real balls.
    """

    conn = manifest.conn

    # Reserve slots for random high-confidence verification
    verify_slots = max(TILES_PER_GRID, max_tiles // 5)  # ~20%, at least one grid
    uncertain_slots = max_tiles - verify_slots

    # Uncertain/low-confidence tiles (main QA work)
    uncertain_rows = conn.execute(
        """SELECT DISTINCT l.tile_stem, l.confidence
           FROM labels l
           WHERE l.qa_verdict IS NULL AND l.confidence < 0.6
             AND l.source != 'sonnet_qa_discovery'
           ORDER BY
               CASE
                   WHEN l.confidence BETWEEN 0.3 AND 0.6 THEN 0
                   WHEN l.confidence < 0.3 THEN 1
                   ELSE 2
               END,
               l.confidence ASC
           LIMIT ?""",
        (uncertain_slots,),
    ).fetchall()

    # Random sample of high-confidence un-QA'd detections (track validation)
    high_conf_rows = conn.execute(
        """SELECT DISTINCT l.tile_stem, l.confidence
           FROM labels l
           WHERE l.qa_verdict IS NULL AND l.confidence >= 0.6
             AND l.source != 'sonnet_qa_discovery'
           ORDER BY RANDOM()
           LIMIT ?""",
        (verify_slots,),
    ).fetchall()

    candidates = [{"tile_stem": r[0], "confidence": r[1]} for r in uncertain_rows]
    verify = [{"tile_stem": r[0], "confidence": r[1]} for r in high_conf_rows]

    if verify:
        logger.info(
            "QA candidates: %d uncertain + %d high-conf verification",
            len(candidates),
            len(verify),
        )

    # Phase-aware filtering: de-prioritize non-play tiles (cap at 10%)
    import re

    phases = manifest.get_phases()
    if phases:
        play_candidates = []
        nonplay_candidates = []
        for c in candidates:
            m = re.match(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$", c["tile_stem"])
            if m and not manifest.is_active_play(m.group(1), int(m.group(2))):
                nonplay_candidates.append(c)
            else:
                play_candidates.append(c)

        # Cap non-play tiles at 10% of the total
        nonplay_cap = max(TILES_PER_GRID, max_tiles // 10)
        nonplay_candidates = nonplay_candidates[:nonplay_cap]
        candidates = play_candidates + nonplay_candidates

        if nonplay_candidates:
            logger.info(
                "Phase filter: %d play + %d non-play candidates (capped from %d)",
                len(play_candidates),
                len(nonplay_candidates),
                len(nonplay_candidates),
            )

    # Interleave: put verify tiles every ~5 candidates so they're spread across grids
    combined = []
    v_idx = 0
    for i, c in enumerate(candidates):
        combined.append(c)
        if (i + 1) % 5 == 0 and v_idx < len(verify):
            combined.append(verify[v_idx])
            v_idx += 1
    # Append remaining verify tiles
    combined.extend(verify[v_idx:])

    return combined


def _build_grids(
    candidates: list[dict],
    manifest,
    packs_dir: Path,
    output_dir: Path,
) -> list[dict]:
    """Build composite grid images from tile candidates.

    Each grid is a 3x2 image with numbered tiles.
    Returns list of {"image_path": Path, "tile_stems": [str]}
    """
    import cv2
    import numpy as np

    grids = []
    tile_size = 640
    output_dir.mkdir(parents=True, exist_ok=True)

    for grid_start in range(0, len(candidates), TILES_PER_GRID):
        batch = candidates[grid_start : grid_start + TILES_PER_GRID]

        # Create composite image
        composite = np.zeros(
            (tile_size * GRID_ROWS, tile_size * GRID_COLS, 3), dtype=np.uint8
        )
        tile_stems = []

        for idx, cand in enumerate(batch):
            row = idx // GRID_COLS
            col = idx % GRID_COLS

            # Read tile from pack
            tile_stem = cand["tile_stem"]
            tile_stems.append(tile_stem)

            jpeg_bytes = _read_tile_from_packs(manifest, tile_stem, packs_dir)
            if jpeg_bytes is None:
                continue

            img_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            if img_arr.size == 0:
                logger.warning("Empty tile data for %s, skipping", tile_stem)
                continue
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue

            # Resize if needed
            if img.shape[:2] != (tile_size, tile_size):
                img = cv2.resize(img, (tile_size, tile_size))

            y = row * tile_size
            x = col * tile_size
            composite[y : y + tile_size, x : x + tile_size] = img

            # Add number label
            cv2.putText(
                composite,
                str(idx + 1),
                (x + 10, y + 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.5,
                (0, 255, 0),
                3,
            )

        # Save composite to local work dir
        grid_path = output_dir / f"qa_grid_{grid_start}.jpg"
        cv2.imwrite(str(grid_path), composite, [cv2.IMWRITE_JPEG_QUALITY, 85])
        grids.append({"image_path": grid_path, "tile_stems": tile_stems})

    return grids


def _read_tile_from_packs(manifest, tile_stem: str, local_packs: Path) -> bytes | None:
    """Read a tile's JPEG bytes from its pack file."""
    import re

    # Parse tile_stem to get segment, frame, row, col
    m = re.match(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$", tile_stem)
    if not m:
        return None

    segment = m.group(1)
    frame_idx = int(m.group(2))
    row = int(m.group(3))
    col = int(m.group(4))

    tile = manifest.get_tile(segment, frame_idx, row, col)
    if not tile or not tile.get("pack_file"):
        return None

    # Try local pack first, then original path, then F: archive
    pack_name = Path(tile["pack_file"]).name
    local_pack = local_packs / pack_name
    if not local_pack.exists():
        local_pack = Path(tile["pack_file"])
    if not local_pack.exists():
        # Try F: archive
        from training.data_prep.manifest_dataset import _resolve_pack_path

        try:
            local_pack = Path(_resolve_pack_path(tile["pack_file"]))
        except FileNotFoundError:
            return None

    try:
        with open(local_pack, "rb") as f:
            f.seek(tile["pack_offset"])
            return f.read(tile["pack_size"])
    except Exception:
        return None


def _call_claude(image_path: Path, tile_stems: list[str]) -> dict[str, str]:
    """Call claude CLI with a composite grid image for QA.

    Returns dict mapping tile_stem -> "BALL" or "NOT_BALL".
    """
    n = len(tile_stems)
    # Include file path in prompt so Claude uses Read tool to view the image
    prompt = (
        f"Read the image at {image_path} and analyze it. "
        f"This image shows a {GRID_COLS}x{GRID_ROWS} grid of {n} numbered soccer field tiles. "
        f"Each tile is 640x640 pixels from a panoramic camera. "
        f"For each numbered tile (1-{n}), determine if there is a soccer ball visible. "
        f"Respond with ONLY a JSON object mapping tile number to verdict. Example:\n"
        f'{{"1": "BALL", "2": "NOT_BALL", "3": "BALL"}}\n'
        f"A soccer ball is typically 8-40 pixels, white/black, roughly circular. "
        f"Ignore players, lines, shadows, and other objects."
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--allowedTools",
                "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning(
                "claude CLI failed (rc=%d): stderr=%s stdout=%s",
                result.returncode,
                result.stderr[:300],
                result.stdout[:300],
            )
            return {}

        # Parse response — extract JSON from output
        output = result.stdout.strip()
        if not output:
            logger.warning("claude CLI returned empty output")
            return {}

        # Try to find JSON in the output
        response_data = _extract_json(output)
        if not response_data:
            logger.warning(
                "Could not parse claude response (len=%d): %s",
                len(output),
                output[:500],
            )
            return {}

        # Map numbered results back to tile_stems
        verdicts = {}
        for i, stem in enumerate(tile_stems):
            key = str(i + 1)
            verdict = response_data.get(key, "")
            if isinstance(verdict, str):
                verdicts[stem] = verdict.upper()

        return verdicts

    except subprocess.TimeoutExpired:
        logger.warning("claude CLI timed out")
        return {}
    except Exception as e:
        logger.warning("claude CLI error: %s", e)
        return {}


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from potentially messy CLI output."""
    import re

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            # Handle claude --output-format json wrapping
            if "result" in data:
                inner = data["result"]
                if isinstance(inner, str):
                    return _extract_json(inner)
                return inner
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # Strip markdown code fences (```json ... ```)
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # Try to find JSON object in the text
    match = re.search(r"\{[^{}]+\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _call_claude_gap(filmstrip_path: Path, context_frames: list[dict]) -> str | None:
    """Call Claude with a trajectory gap filmstrip.

    Shows Sonnet a sequence of frames with ball positions marked, plus
    the gap frame where the ball should be. Returns "FOUND" or "NOT_FOUND".
    """
    n_frames = len(context_frames)
    n_before = sum(1 for f in context_frames if f["role"] == "before")
    gap_frame_num = n_before + 1  # 1-indexed position of the gap frame

    prompt = (
        f"Read the image at {filmstrip_path} and analyze it. "
        f"This filmstrip shows {n_frames} consecutive frames from a soccer camera tracking a ball. "
        f"The frames are arranged left to right in time order. "
        f"Red circles mark where the ball was confirmed in earlier frames. "
        f"The yellow circle with '?' in frame {gap_frame_num} marks where the ball SHOULD be "
        f"based on its trajectory, but the detector lost it. "
        f"Look carefully near the yellow marker — can you see a soccer ball there? "
        f"It would be 8-40 pixels, white/black, roughly circular. "
        f"Respond with ONLY the word FOUND or NOT_FOUND."
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--allowedTools",
                "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning(
                "claude CLI gap call failed (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
            return None

        output = result.stdout.strip()
        if not output:
            return None

        # Parse the response — extract FOUND/NOT_FOUND from JSON wrapper
        data = _extract_json(output)
        if data and isinstance(data, dict):
            text = data.get("result", "")
        else:
            text = output

        text = str(text).upper().strip()
        if "FOUND" in text and "NOT" not in text:
            return "FOUND"
        elif "NOT_FOUND" in text or "NOT FOUND" in text:
            return "NOT_FOUND"
        else:
            logger.warning("Unexpected gap response: %s", text[:100])
            return "NOT_FOUND"

    except subprocess.TimeoutExpired:
        logger.warning("claude CLI gap call timed out")
        return None
    except Exception as e:
        logger.warning("claude CLI gap call error: %s", e)
        return None


def _get_trajectory_sample_frames(
    traj: list[tuple[int, str, float, float]],
    n_samples: int = 5,
    frame_interval: int = 4,
    manifest=None,
) -> list[dict]:
    """Get evenly-spaced frames from a trajectory for verification.

    If manifest is provided, only returns frames that actually have tiles
    in the manifest (some games are partially tiled).

    Returns frame dicts compatible with build_gap_filmstrip (role='before').
    """
    from training.data_prep.trajectory_gaps import _pano_to_tile

    if len(traj) < 3:
        return []

    # Filter to frames that have tiles if manifest is available
    if manifest:
        tiled_frames = set()
        for fi, seg, _, _ in traj:
            fi_snapped = round(fi / frame_interval) * frame_interval
            count = manifest.conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE segment = ? AND frame_idx = ?",
                (seg, fi_snapped),
            ).fetchone()[0]
            if count > 0:
                tiled_frames.add((fi_snapped, seg))
        # Rebuild traj with only tiled frames
        traj_filtered = []
        for fi, seg, px, py in traj:
            fi_snapped = round(fi / frame_interval) * frame_interval
            if (fi_snapped, seg) in tiled_frames:
                traj_filtered.append((fi_snapped, seg, px, py))
        if len(traj_filtered) >= 3:
            traj = traj_filtered

    # Pick evenly-spaced indices
    step = max(1, (len(traj) - 1) // (n_samples - 1))
    indices = list(range(0, len(traj), step))[:n_samples]
    if indices[-1] != len(traj) - 1:
        indices[-1] = len(traj) - 1

    frames = []
    seen_fi = set()
    for idx in indices:
        fi, seg, px, py = traj[idx]
        fi = round(fi / frame_interval) * frame_interval
        if fi in seen_fi:
            continue
        seen_fi.add(fi)

        tile_info = _pano_to_tile(px, py)
        if tile_info is None:
            continue
        row, col, cx_norm, cy_norm = tile_info
        frames.append(
            {
                "frame_idx": fi,
                "segment": seg,
                "pano_x": px,
                "pano_y": py,
                "role": "before",
                "tile_stem": f"{seg}_frame_{fi:06d}_r{row}_c{col}",
                "tile_local_x": cx_norm,
                "tile_local_y": cy_norm,
            }
        )

    return frames


def _verify_trajectory_with_sonnet(filmstrip_path: Path, traj_length: int) -> bool:
    """Ask Sonnet to verify that a trajectory filmstrip shows a game ball.

    Returns True if Sonnet confirms this is a soccer ball moving on the field.
    """
    prompt = (
        f"Read the image at {filmstrip_path} and analyze it. "
        f"This filmstrip shows {min(5, traj_length)} frames from a panoramic soccer camera. "
        f"Red circles mark detected objects across consecutive frames. "
        f"Is this a SOCCER BALL moving across the playing field during a game? "
        f"Consider: A real game ball moves significantly between frames, is 8-40 pixels, "
        f"white/black, and is ON the playing field (green grass), not on the sideline. "
        f"Reject if it's: a static ball, equipment, player, shadow, or anything off the field. "
        f"Respond with ONLY the word GAME_BALL or NOT_GAME_BALL."
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--allowedTools",
                "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning("Trajectory verify failed (rc=%d)", result.returncode)
            return False

        output = result.stdout.strip()
        if not output:
            return False

        data = _extract_json(output)
        if data and isinstance(data, dict):
            text = str(data.get("result", ""))
        else:
            text = output

        text = text.upper().strip()
        return "GAME_BALL" in text and "NOT_GAME_BALL" not in text

    except Exception as e:
        logger.warning("Trajectory verify error: %s", e)
        return False
