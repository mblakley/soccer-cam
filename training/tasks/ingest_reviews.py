"""Ingest reviews task — collect human verdicts and update per-game manifests.

After humans review tiles via the annotation server, this task:
1. Reads review results from review_packets/{packet}/annotation_results.json
2. Updates qa_verdict in per-game manifest.db
3. Moves processed packets to archive
4. If enough corrections accumulated, triggers training set rebuild

Pull-local-process-push pattern:
  - Pull: review results from server
  - Process: parse verdicts, update local manifest copy
  - Push: updated manifest.db back to server
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("ingest_reviews")
def run_ingest_reviews(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Ingest human review verdicts into per-game manifests."""
    payload = item.get("payload") or {}

    from training.pipeline.config import load_config

    cfg = load_config()

    review_packets_dir = Path("review_packets")
    if not review_packets_dir.exists():
        return {"packets_processed": 0, "verdicts_applied": 0}

    total_verdicts = 0
    total_packets = 0
    games_updated = set()

    for packet_dir in sorted(review_packets_dir.iterdir()):
        if not packet_dir.is_dir():
            continue

        # Check for results
        results_file = packet_dir / "annotation_results.json"
        manifest_file = packet_dir / "manifest.json"

        if not results_file.exists():
            continue  # Not yet reviewed

        if not manifest_file.exists():
            continue

        # Read packet manifest to get game_id
        packet_manifest = json.loads(manifest_file.read_text())
        game_id = packet_manifest.get("game_id")
        if not game_id:
            continue

        # Read results
        results = json.loads(results_file.read_text())
        if not isinstance(results, list):
            results = results.get("results", [])

        if not results:
            continue

        # Pull game manifest to local
        server_game_dir = Path(cfg.paths.games_dir) / game_id
        if server_share and not server_game_dir.exists():
            server_game_dir = Path(server_share) / "games" / game_id

        local_game = local_work_dir / game_id
        local_game.mkdir(parents=True, exist_ok=True)

        server_manifest = server_game_dir / "manifest.db"
        if not server_manifest.exists():
            logger.warning("No manifest for %s, skipping packet %s", game_id, packet_dir.name)
            continue

        shutil.copy2(str(server_manifest), str(local_game / "manifest.db"))

        from training.data_prep.game_manifest import GameManifest

        manifest = GameManifest(local_game)
        manifest.open(create=False)

        # Apply verdicts
        verdicts_applied = 0
        for result in results:
            # Handle different result formats from annotation server
            frame_idx = result.get("frame_idx")
            action = result.get("action", "")
            tile_stem = result.get("tile_stem", "")

            # Map action to qa_verdict
            if action in ("confirm", "adjust"):
                verdict = "true_positive"
            elif action in ("reject", "not_visible"):
                verdict = "false_positive"
            elif action == "locate":
                verdict = "true_positive"  # Human found the ball
            else:
                continue  # skip

            if tile_stem:
                manifest.set_qa_verdict(tile_stem, verdict)
                verdicts_applied += 1
            elif frame_idx is not None:
                # Try to find tile stem from packet items
                for pitem in packet_manifest.get("items", []):
                    if pitem.get("frame_idx") == frame_idx:
                        manifest.set_qa_verdict(pitem["tile_stem"], verdict)
                        verdicts_applied += 1
                        break

        manifest.set_metadata("reviews_ingested_at", str(time.time()))
        manifest.close()

        # Push updated manifest
        shutil.copy2(str(local_game / "manifest.db"), str(server_manifest))

        # Archive the processed packet
        archive_dir = review_packets_dir / "_processed"
        archive_dir.mkdir(exist_ok=True)
        shutil.move(str(packet_dir), str(archive_dir / packet_dir.name))

        total_verdicts += verdicts_applied
        total_packets += 1
        games_updated.add(game_id)

        logger.info(
            "Ingested %d verdicts from %s for %s",
            verdicts_applied, packet_dir.name, game_id,
        )

    logger.info(
        "Ingest complete: %d packets, %d verdicts, %d games updated",
        total_packets, total_verdicts, len(games_updated),
    )

    return {
        "packets_processed": total_packets,
        "verdicts_applied": total_verdicts,
        "games_updated": list(games_updated),
    }
