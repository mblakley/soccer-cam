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
import os
import subprocess
import tempfile
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
    payload = item.get("payload") or {}

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

    candidates = _get_qa_candidates(manifest, max_tiles=cfg.qa.sonnet_batch_limit * cfg.qa.sonnet_batch_size)

    if not candidates:
        manifest.close()
        logger.info("No QA candidates for %s", game_id)
        return {"tiles_reviewed": 0, "verdicts": {}}

    logger.info("QA: %d candidate tiles for %s", len(candidates), game_id)

    # Step 3: Pull only the pack files that QA candidates reference to SSD
    needed_packs = _find_needed_packs(candidates, manifest)
    _pull_selective_packs(task_io, needed_packs)
    packs_dir = task_io.local_packs

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
                grid_num, max_batches, n_tiles, grid_info["image_path"].name,
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
                    grid_num, balls, n_tiles, elapsed,
                    total_reviewed, verdicts["ball"], verdicts["not_ball"],
                )

            except Exception as e:
                logger.exception("Claude QA grid %d failed: %s", grid_num, e)
                verdicts["error"] += n_tiles

            # Brief pause between API calls
            time.sleep(2)

    manifest.set_metadata("qa_at", str(time.time()))
    manifest.close()

    # Step 5: Push updated manifest back
    task_io.push_manifest()

    logger.info(
        "QA complete for %s: %d reviewed (ball=%d, not_ball=%d, error=%d)",
        game_id, total_reviewed, verdicts["ball"], verdicts["not_ball"], verdicts["error"],
    )

    return {
        "tiles_reviewed": total_reviewed,
        "verdicts": verdicts,
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
    server_packs = task_io.server_packs()
    copied = 0
    for pack_path_str in pack_files:
        pack_name = Path(pack_path_str).name
        src = server_packs / pack_name
        dest = task_io.local_packs / pack_name
        src_size = src.stat().st_size if src.exists() else 0
        if dest.exists() and dest.stat().st_size == src_size:
            logger.info("Pack %s already on SSD (%.1f GB), skipping copy", pack_name, src_size / (1024**3))
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


def _get_qa_candidates(manifest, max_tiles: int = 2000) -> list[dict]:
    """Get tiles that need QA — prioritize uncertain detections."""
    conn = manifest.conn

    # Tiles with labels but no QA verdict
    rows = conn.execute(
        """SELECT DISTINCT l.tile_stem, l.confidence
           FROM labels l
           WHERE l.qa_verdict IS NULL
           ORDER BY
               CASE
                   WHEN l.confidence BETWEEN 0.3 AND 0.6 THEN 0  -- uncertain first
                   WHEN l.confidence < 0.3 THEN 1                 -- low conf
                   ELSE 2                                          -- high conf
               END,
               l.confidence ASC
           LIMIT ?""",
        (max_tiles,),
    ).fetchall()

    return [{"tile_stem": r[0], "confidence": r[1]} for r in rows]


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

    # Try local pack first
    pack_name = Path(tile["pack_file"]).name
    local_pack = local_packs / pack_name
    if not local_pack.exists():
        # Try original path
        local_pack = Path(tile["pack_file"])
    if not local_pack.exists():
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
                "claude", "-p", prompt,
                "--output-format", "json",
                "--model", "sonnet",
                "--allowedTools", "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning("claude CLI failed (rc=%d): stderr=%s stdout=%s",
                           result.returncode, result.stderr[:300], result.stdout[:300])
            return {}

        # Parse response — extract JSON from output
        output = result.stdout.strip()
        if not output:
            logger.warning("claude CLI returned empty output")
            return {}

        # Try to find JSON in the output
        response_data = _extract_json(output)
        if not response_data:
            logger.warning("Could not parse claude response (len=%d): %s", len(output), output[:500])
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
