"""Generate review task — package trajectory gap tiles for human review + NTFY.

After Sonnet QA, trajectory endpoints where both the detector AND Sonnet
failed to find the ball are the highest-value frames for human review.
The human can either locate the ball (new training label) or confirm
it went out of play (autocam data).

Pull-local-process-push pattern:
  - Pull: manifest.db + packs for the game
  - Process: extract gap tiles, build filmstrip composites, create review packet
  - Push: review packet to server's review_packets/ dir
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
    """Package trajectory gap tiles into a review packet for human review."""
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

    # Find tiles needing human review — trajectory gaps where Sonnet failed
    candidates = _get_review_candidates(manifest)

    if not candidates:
        manifest.close()
        logger.info("No review candidates for %s", game_id)
        return {"review_count": 0}

    logger.info("Review: %d candidates for %s", len(candidates), game_id)

    # Pull needed packs for extracting tiles and building filmstrips
    from training.tasks.sonnet_qa import _find_needed_packs, _pull_selective_packs

    needed_packs = _find_needed_packs(candidates, manifest)
    _pull_selective_packs(task_io, needed_packs)

    # Build review packet
    review_dir = Path(cfg.paths.games_dir).parent / "review_packets" / f"{game_id}_{int(time.time())}"
    review_dir.mkdir(parents=True, exist_ok=True)

    from training.tasks.sonnet_qa import _read_tile_from_packs

    extracted = 0
    manifest_items = []

    # Load gap positions metadata (saved by sonnet_qa Phase 2)
    gap_positions_raw = manifest.conn.execute(
        "SELECT value FROM metadata WHERE key = 'gap_positions'"
    ).fetchone()
    gap_positions = json.loads(gap_positions_raw[0]) if gap_positions_raw else {}

    for cand in candidates:
        tile_stem = cand["tile_stem"]

        # Save the tile image
        jpeg_bytes = _read_tile_from_packs(manifest, tile_stem, task_io.local_packs)
        if jpeg_bytes is None:
            continue

        tile_path = review_dir / f"{tile_stem}.jpg"
        tile_path.write_bytes(jpeg_bytes)

        # Include gap context if available
        gap_ctx = gap_positions.get(tile_stem, {})

        manifest_items.append({
            "tile_stem": tile_stem,
            "qa_verdict": cand.get("qa_verdict"),
            "priority": cand.get("priority", 0),
            "reason": cand.get("reason", ""),
            "gap_context": gap_ctx if gap_ctx else None,
        })
        extracted += 1

    # Also build filmstrips for gap candidates (trajectory context)
    _build_review_filmstrips(candidates, manifest, task_io.local_packs, review_dir, cfg)

    # Write review manifest
    review_manifest = {
        "game_id": game_id,
        "created_at": time.time(),
        "tile_count": extracted,
        "items": manifest_items,
    }
    (review_dir / "manifest.json").write_text(json.dumps(review_manifest, indent=2))

    manifest.close()

    # Send NTFY notification
    if extracted > 0 and cfg.ntfy.enabled:
        gap_count = sum(1 for c in candidates if c.get("reason", "").startswith("trajectory_gap"))
        try:
            subprocess.run(
                [
                    "curl", "-s",
                    "-H", "Title: Gap Review Ready",
                    "-H", "Priority: high",
                    "-H", f"Tags: soccer,{game_id}",
                    "-d",
                    f"{extracted} trajectory gaps need review\n"
                    f"Game: {game_id}\n"
                    f"{gap_count} gaps where ball disappeared\n"
                    f"Review at: https://trainer.goat-rattlesnake.ts.net/static/annotate.html#gap-review",
                    f"https://ntfy.sh/{cfg.ntfy.topic}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)

    logger.info("Generated review packet for %s: %d tiles (%d gaps)", game_id, extracted, gap_count)

    return {
        "review_count": extracted,
        "review_dir": str(review_dir),
    }


def _get_review_candidates(manifest, max_tiles: int = 100) -> list[dict]:
    """Get tiles for human review — trajectory gaps where Sonnet also failed.

    Priority:
    1. gap_no_ball: trajectory endpoints where Sonnet couldn't find ball (200)
    2. Sonnet disagreement: false_positive on high-confidence detection (100)
    """
    conn = manifest.conn
    candidates = []

    # HIGHEST PRIORITY: trajectory gaps where Sonnet couldn't find the ball
    # These are the most valuable — if human finds ball, it's a new training label
    gap_tiles = conn.execute(
        """SELECT tile_stem, confidence, qa_verdict
           FROM labels
           WHERE qa_verdict = 'gap_no_ball'
           LIMIT ?""",
        (max_tiles,),
    ).fetchall()
    for r in gap_tiles:
        candidates.append({
            "tile_stem": r[0],
            "confidence": r[1],
            "qa_verdict": r[2],
            "priority": 200,
            "reason": "trajectory_gap_sonnet_failed",
        })

    # MEDIUM PRIORITY: Sonnet disagreed with high-confidence model detection
    remaining = max_tiles - len(candidates)
    if remaining > 0:
        disagreements = conn.execute(
            """SELECT tile_stem, confidence, qa_verdict
               FROM labels
               WHERE qa_verdict = 'false_positive' AND confidence > 0.6
               LIMIT ?""",
            (remaining,),
        ).fetchall()
        for r in disagreements:
            candidates.append({
                "tile_stem": r[0],
                "confidence": r[1],
                "qa_verdict": r[2],
                "priority": 100,
                "reason": "sonnet_disagreement",
            })

    candidates.sort(key=lambda x: x["priority"], reverse=True)
    return candidates[:max_tiles]


def _build_review_filmstrips(candidates, manifest, packs_dir, review_dir, cfg):
    """Build filmstrip composites for gap candidates (same format as Sonnet saw)."""
    try:
        from training.data_prep.trajectory_gaps import (
            build_trajectories_from_manifest,
            find_gap_candidates,
            get_gap_context_frames,
            build_gap_filmstrip,
        )

        trajectories = build_trajectories_from_manifest(
            manifest.conn, min_length=cfg.qa.min_trajectory_length,
        )
        if not trajectories:
            return

        gaps = find_gap_candidates(trajectories, frame_interval=cfg.tiling.frame_interval)
        gap_stems = {c["tile_stem"] for c in candidates if c["reason"] == "trajectory_gap_sonnet_failed"}

        filmstrip_dir = review_dir / "filmstrips"
        filmstrip_dir.mkdir(exist_ok=True)

        built = 0
        for gap in gaps:
            traj = trajectories[gap["trajectory_idx"]]
            context = get_gap_context_frames(gap, traj, n_before=3, n_after=2)
            gap_frame = next((f for f in context if f["role"] == "gap"), None)
            if not gap_frame or gap_frame["tile_stem"] not in gap_stems:
                continue

            out = filmstrip_dir / f"{gap_frame['tile_stem']}_filmstrip.jpg"
            if build_gap_filmstrip(context, manifest, packs_dir, out):
                built += 1

        logger.info("Built %d filmstrips for review", built)

    except Exception as e:
        logger.warning("Failed to build review filmstrips: %s", e)
