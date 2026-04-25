"""Migrate monolithic manifest.db to per-game manifests + registry.db.

One-time migration script. Non-destructive — old manifest.db is kept as backup.

What it does:
1. Reads all games from monolithic manifest.db
2. For each game, creates games/{game_id}/manifest.db with tiles + labels
3. Creates registry.db with game metadata + pipeline states
4. Optionally moves pack files to per-game directories

Usage:
    uv run python -m training.pipeline.migrate
    uv run python -m training.pipeline.migrate --dry-run
    uv run python -m training.pipeline.migrate --games flash__2024.06.01_vs_IYSA_home
    uv run python -m training.pipeline.migrate --move-packs
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def migrate(
    old_db_path: Path,
    games_dir: Path,
    registry_db_path: Path,
    game_registry_json: Path | None = None,
    old_pack_dir: Path | None = None,
    move_packs: bool = False,
    game_filter: list[str] | None = None,
    dry_run: bool = False,
):
    """Migrate monolithic manifest.db → per-game DBs + registry.db."""

    if not old_db_path.exists():
        logger.error("Source manifest not found: %s", old_db_path)
        return

    old_conn = sqlite3.connect(str(old_db_path))
    old_conn.row_factory = sqlite3.Row

    # Load game registry JSON if available (for additional metadata)
    registry_meta = {}
    if game_registry_json and game_registry_json.exists():
        with open(game_registry_json) as f:
            registry_data = json.load(f)
        # game_registry.json is a list of game dicts
        if isinstance(registry_data, list):
            game_list = registry_data
        else:
            game_list = registry_data.get("games", [])
        for game in game_list:
            gid = game.get("game_id", "")
            if gid:
                registry_meta[gid] = game

    # Get all games from old DB
    games = old_conn.execute(
        "SELECT * FROM games ORDER BY game_id"
    ).fetchall()
    games = [dict(g) for g in games]

    if game_filter:
        games = [g for g in games if g["game_id"] in game_filter]

    logger.info("Migrating %d games from %s", len(games), old_db_path)
    logger.info("Output: games → %s, registry → %s", games_dir, registry_db_path)

    if dry_run:
        for g in games:
            tc = old_conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE game_id = ?", (g["game_id"],)
            ).fetchone()[0]
            lc = old_conn.execute(
                "SELECT COUNT(*) FROM labels WHERE game_id = ?", (g["game_id"],)
            ).fetchone()[0]
            logger.info("  [DRY RUN] %s: %d tiles, %d labels", g["game_id"], tc, lc)
        old_conn.close()
        return

    # Create registry DB
    from training.pipeline.registry import GameRegistry
    from training.pipeline.state_machine import infer_initial_state

    registry = GameRegistry(registry_db_path)

    # Process each game
    for gi, game in enumerate(games):
        game_id = game["game_id"]

        # Skip non-game entries (like .locks)
        if game_id.startswith("."):
            logger.info("  Skipping %s (not a game)", game_id)
            continue

        t0 = time.time()
        logger.info("[%d/%d] Migrating %s...", gi + 1, len(games), game_id)

        # Count tiles and labels in old DB
        tile_count = old_conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE game_id = ?", (game_id,)
        ).fetchone()[0]
        label_count = old_conn.execute(
            "SELECT COUNT(*) FROM labels WHERE game_id = ?", (game_id,)
        ).fetchone()[0]

        # Create per-game directory and manifest
        game_dir = games_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)
        game_db_path = game_dir / "manifest.db"

        # Skip if already migrated
        if game_db_path.exists():
            existing_size = os.path.getsize(game_db_path)
            if existing_size > 4096:  # not just schema
                logger.info("  Already migrated (%d bytes), skipping", existing_size)
                # Still register in registry
                _register_game(registry, game_id, game, registry_meta, tile_count, label_count)
                continue

        # Create per-game SQLite DB
        new_conn = sqlite3.connect(str(game_db_path))
        new_conn.execute("PRAGMA journal_mode=WAL")
        new_conn.execute("PRAGMA synchronous=NORMAL")

        # Create schema (from game_manifest module)
        from training.data_prep.game_manifest import SCHEMA_SQL
        new_conn.executescript(SCHEMA_SQL)

        # Copy segments
        segments = old_conn.execute(
            "SELECT segment, frame_count, tile_count, frame_min, frame_max, max_gap "
            "FROM segments WHERE game_id = ?", (game_id,)
        ).fetchall()
        if segments:
            new_conn.executemany(
                "INSERT INTO segments (segment, frame_count, tile_count, frame_min, frame_max, max_gap) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(s[0], s[1], s[2], s[3], s[4], s[5]) for s in segments],
            )

        # Copy frames
        frames = old_conn.execute(
            "SELECT segment, frame_idx, tile_count FROM frames WHERE game_id = ?",
            (game_id,),
        ).fetchall()
        if frames:
            new_conn.executemany(
                "INSERT INTO frames (segment, frame_idx, tile_count) VALUES (?, ?, ?)",
                [(f[0], f[1], f[2]) for f in frames],
            )

        # Copy tiles (in batches for memory)
        BATCH = 50000
        offset = 0
        total_tiles = 0
        while True:
            tiles = old_conn.execute(
                "SELECT segment, frame_idx, row, col, pack_file, pack_offset, pack_size "
                "FROM tiles WHERE game_id = ? LIMIT ? OFFSET ?",
                (game_id, BATCH, offset),
            ).fetchall()
            if not tiles:
                break

            # Rewrite pack_file paths if we're moving packs
            tile_rows = []
            for t in tiles:
                pack_file = t[4]
                if pack_file and move_packs:
                    # Rewrite: D:/training_data/tile_packs/{game_id}/{seg}.pack
                    # → D:/training_data/games/{game_id}/tile_packs/{seg}.pack
                    old_pack = Path(pack_file)
                    new_pack = game_dir / "tile_packs" / old_pack.name
                    pack_file = str(new_pack)
                tile_rows.append((t[0], t[1], t[2], t[3], pack_file, t[5], t[6]))

            new_conn.executemany(
                "INSERT INTO tiles (segment, frame_idx, row, col, pack_file, pack_offset, pack_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                tile_rows,
            )
            total_tiles += len(tiles)
            offset += BATCH

        # Copy labels (in batches)
        offset = 0
        total_labels = 0
        while True:
            labels = old_conn.execute(
                "SELECT tile_stem, class_id, cx, cy, w, h, source, confidence "
                "FROM labels WHERE game_id = ? LIMIT ? OFFSET ?",
                (game_id, BATCH, offset),
            ).fetchall()
            if not labels:
                break
            new_conn.executemany(
                "INSERT OR IGNORE INTO labels (tile_stem, class_id, cx, cy, w, h, source, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(l[0], l[1], l[2], l[3], l[4], l[5], l[6], l[7]) for l in labels],
            )
            total_labels += len(labels)
            offset += BATCH

        new_conn.commit()

        # Move pack files if requested
        if move_packs and old_pack_dir:
            old_game_packs = old_pack_dir / game_id
            new_game_packs = game_dir / "tile_packs"
            if old_game_packs.exists():
                new_game_packs.mkdir(parents=True, exist_ok=True)
                for pack_file in old_game_packs.glob("*.pack"):
                    dest = new_game_packs / pack_file.name
                    if not dest.exists():
                        shutil.move(str(pack_file), str(dest))

        new_conn.close()

        elapsed = time.time() - t0
        db_size = os.path.getsize(game_db_path)
        logger.info(
            "  %d tiles, %d labels → %s (%.1f MB, %.1fs)",
            total_tiles, total_labels, game_db_path,
            db_size / 1e6, elapsed,
        )

        # Register in registry
        _register_game(registry, game_id, game, registry_meta, total_tiles, total_labels)

    registry.close()
    old_conn.close()
    logger.info("Migration complete. Registry: %s", registry_db_path)


def _register_game(
    registry,
    game_id: str,
    game_row: dict,
    registry_meta: dict,
    tile_count: int,
    label_count: int,
):
    """Register a game in the registry DB with inferred state."""
    from training.pipeline.state_machine import infer_initial_state

    meta = registry_meta.get(game_id, {})

    # Infer state from data
    has_packs = tile_count > 0
    has_labels = label_count > 0
    trainable = meta.get("trainable", True)
    if isinstance(trainable, str):
        trainable = trainable.lower() not in ("false", "0", "no")

    state = infer_initial_state(
        has_video=True,
        has_packs=has_packs,
        has_labels=has_labels,
        trainable=trainable,
    )

    registry.register_game(
        game_id,
        team=meta.get("team"),
        date=meta.get("date"),
        opponent=meta.get("opponent"),
        location=meta.get("location"),
        video_path=meta.get("video_path") or meta.get("path"),
        needs_flip=meta.get("needs_flip", False) or meta.get("orientation") == "upside_down",
        game_type=meta.get("game_type"),
        camera_type=meta.get("camera_type", "dahua"),
        trainable=trainable,
        pipeline_state=state,
    )
    registry.update_stats(
        game_id,
        tile_count=tile_count,
        label_count=label_count,
    )


def main():
    parser = argparse.ArgumentParser(description="Migrate monolithic manifest to per-game DBs")
    parser.add_argument("--source", default="D:/training_data/manifest.db",
                        help="Path to monolithic manifest.db")
    parser.add_argument("--games-dir", default="D:/training_data/games",
                        help="Output directory for per-game manifests")
    parser.add_argument("--registry", default="D:/training_data/registry.db",
                        help="Output path for registry.db")
    parser.add_argument("--game-registry-json", default="D:/training_data/game_registry.json",
                        help="Path to game_registry.json for metadata")
    parser.add_argument("--old-pack-dir", default="D:/training_data/tile_packs",
                        help="Path to existing pack files")
    parser.add_argument("--move-packs", action="store_true",
                        help="Move pack files to per-game directories")
    parser.add_argument("--games", nargs="*", help="Only migrate specific games")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be migrated without doing it")
    args = parser.parse_args()

    migrate(
        old_db_path=Path(args.source),
        games_dir=Path(args.games_dir),
        registry_db_path=Path(args.registry),
        game_registry_json=Path(args.game_registry_json) if args.game_registry_json else None,
        old_pack_dir=Path(args.old_pack_dir) if args.old_pack_dir else None,
        move_packs=args.move_packs,
        game_filter=args.games,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
