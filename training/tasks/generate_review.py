"""Generate review task — package uncertain tiles for human review + NTFY.

After Sonnet QA, some tiles remain uncertain (disagreements, low confidence).
This task packages the highest-priority ones into review packets and notifies
the user via NTFY.

Pull-local-process-push pattern:
  - Pull: manifest.db + packs for the game
  - Process: extract uncertain tiles, build review packet
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
    """Package uncertain tiles into a review packet for human review."""
    game_id = item["game_id"]
    payload = item.get("payload") or {}

    from training.pipeline.config import load_config

    cfg = load_config()

    # Pull manifest + packs to local SSD
    io = TaskIO(game_id, local_work_dir, server_share)
    io.ensure_space(needed_gb=3)
    io.pull_manifest()
    io.pull_packs()

    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(io.local_game)
    manifest.open(create=False)

    # Find tiles needing human review (prioritized)
    candidates = _get_review_candidates(manifest)

    if not candidates:
        manifest.close()
        logger.info("No review candidates for %s", game_id)
        return {"review_count": 0}

    # Build review packet
    review_dir = Path("review_packets") / f"{game_id}_{int(time.time())}"
    review_dir.mkdir(parents=True, exist_ok=True)

    extracted = 0
    manifest_items = []

    for cand in candidates:
        from training.tasks.sonnet_qa import _read_tile_from_packs

        jpeg_bytes = _read_tile_from_packs(manifest, cand["tile_stem"], io.local_packs)
        if jpeg_bytes is None:
            continue

        # Save tile image
        tile_path = review_dir / f"{cand['tile_stem']}.jpg"
        tile_path.write_bytes(jpeg_bytes)

        manifest_items.append({
            "tile_stem": cand["tile_stem"],
            "confidence": cand.get("confidence"),
            "qa_verdict": cand.get("qa_verdict"),
            "priority": cand.get("priority", 0),
            "reason": cand.get("reason", ""),
        })
        extracted += 1

    # Write manifest
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
        # Count reasons
        reasons = {}
        for item_info in manifest_items:
            r = item_info.get("reason", "other")
            reasons[r] = reasons.get(r, 0) + 1

        reason_str = ", ".join(f"{v} {k}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))

        try:
            subprocess.run(
                [
                    "curl", "-s",
                    "-H", "Title: Tiles Ready for Review",
                    "-H", "Priority: default",
                    "-H", f"Tags: clipboard,{game_id}",
                    "-d",
                    f"{extracted} tiles ready for review\n"
                    f"Game: {game_id}\n"
                    f"Priority breakdown: {reason_str}\n"
                    f"Review at: http://192.168.86.152:8642/ball-verify.html",
                    f"https://ntfy.sh/{cfg.ntfy.topic}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)

    logger.info("Generated review packet for %s: %d tiles", game_id, extracted)

    return {
        "review_count": extracted,
        "review_dir": str(review_dir),
    }


def _get_review_candidates(manifest, max_tiles: int = 100) -> list[dict]:
    """Get tiles that need human review, prioritized.

    Priority scoring:
    1. Sonnet disagreement (model+sonnet disagree) → 100
    2. Low confidence (0.3-0.6) → 60
    3. No QA verdict at all → 40
    """
    conn = manifest.conn

    candidates = []

    # Sonnet disagrees with model (false_positive verdicts on high-conf detections)
    disagreements = conn.execute(
        """SELECT tile_stem, confidence, qa_verdict
           FROM labels
           WHERE qa_verdict = 'false_positive' AND confidence > 0.6
           LIMIT ?""",
        (max_tiles // 3,),
    ).fetchall()
    for r in disagreements:
        candidates.append({
            "tile_stem": r[0],
            "confidence": r[1],
            "qa_verdict": r[2],
            "priority": 100,
            "reason": "sonnet_disagreement",
        })

    # Low confidence, no QA
    low_conf = conn.execute(
        """SELECT tile_stem, confidence, qa_verdict
           FROM labels
           WHERE qa_verdict IS NULL AND confidence BETWEEN 0.3 AND 0.6
           LIMIT ?""",
        (max_tiles // 3,),
    ).fetchall()
    for r in low_conf:
        candidates.append({
            "tile_stem": r[0],
            "confidence": r[1],
            "qa_verdict": r[2],
            "priority": 60,
            "reason": "low_confidence",
        })

    # Random unreviewed
    remaining = max_tiles - len(candidates)
    if remaining > 0:
        unreviewed = conn.execute(
            """SELECT tile_stem, confidence, qa_verdict
               FROM labels
               WHERE qa_verdict IS NULL
               ORDER BY RANDOM()
               LIMIT ?""",
            (remaining,),
        ).fetchall()
        for r in unreviewed:
            candidates.append({
                "tile_stem": r[0],
                "confidence": r[1],
                "qa_verdict": r[2],
                "priority": 40,
                "reason": "unreviewed",
            })

    # Sort by priority descending
    candidates.sort(key=lambda x: x["priority"], reverse=True)
    return candidates[:max_tiles]
