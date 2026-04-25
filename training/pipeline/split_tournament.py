"""Split multi-game tournament manifests into individual per-game manifests.

One-time migration tool. Tournaments recorded as a single game_id containing
multiple games need to be split so each game can have its own phases, QA, and
training pipeline progression.

Usage (via CLI):
    uv run python -m training.pipeline split-tournament --dry-run
    uv run python -m training.pipeline split-tournament --execute --spec splits/heat__2024.06.07.json
    uv run python -m training.pipeline split-tournament --verify --spec splits/heat__2024.06.07.json
    uv run python -m training.pipeline split-tournament --cleanup --spec splits/heat__2024.06.07.json --confirm
"""

import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path

from training.data_prep.game_manifest import GameManifest
from training.pipeline.config import load_config
from training.pipeline.registry import GameRegistry

logger = logging.getLogger(__name__)

# Segment names: 07.45.31-08.02.21[F][0@0][226331]_ch1
_SEG_TIME_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")


def parse_segment_times(segment_name: str) -> tuple[int, int]:
    """Return (start_seconds, end_seconds) from segment name timestamps."""
    m = _SEG_TIME_RE.match(segment_name)
    if not m:
        raise ValueError(f"Cannot parse timestamps from segment: {segment_name}")
    h1, m1, s1, h2, m2, s2 = (int(x) for x in m.groups())
    return (h1 * 3600 + m1 * 60 + s1, h2 * 3600 + m2 * 60 + s2)


def _fmt_time(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_duration(seconds: int) -> str:
    """Format duration for display."""
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def detect_games(
    segments: list[str], gap_threshold_sec: int = 60
) -> list[list[tuple[str, int, int]]]:
    """Group segments into games based on time gaps.

    Returns list of groups, each group is a list of (segment_name, start_sec, end_sec).
    """
    timed = [(seg, *parse_segment_times(seg)) for seg in segments]
    timed.sort(key=lambda x: x[1])  # sort by start time

    groups: list[list[tuple[str, int, int]]] = [[timed[0]]]
    for seg, start, end in timed[1:]:
        prev_end = groups[-1][-1][2]
        if start - prev_end > gap_threshold_sec:
            groups.append([])
        groups[-1].append((seg, start, end))

    return groups


def _get_segment_stats(
    conn: sqlite3.Connection, segments: list[str]
) -> tuple[int, int]:
    """Get tile_count and label_count for a list of segments."""
    if not segments:
        return 0, 0
    seg_ph = ",".join("?" * len(segments))
    tile_count = conn.execute(
        f"SELECT COUNT(*) FROM tiles WHERE segment IN ({seg_ph})", segments
    ).fetchone()[0]

    label_count = 0
    for seg in segments:
        label_count += conn.execute(
            "SELECT COUNT(*) FROM labels WHERE tile_stem LIKE ? || '_frame_%'",
            (seg,),
        ).fetchone()[0]

    return tile_count, label_count


# ---------------------------------------------------------------------------
# Phase 1: Dry-run -- detect games and generate confirmation file
# ---------------------------------------------------------------------------


def dry_run(source_id: str, output_dir: Path) -> Path:
    """Detect game boundaries and generate a confirmation JSON file.

    Returns path to the generated spec file.
    """
    cfg = load_config()
    games_dir = Path(cfg.paths.games_dir)
    source_dir = games_dir / source_id
    manifest_path = source_dir / "manifest.db"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Source manifest not found: {manifest_path}")

    conn = sqlite3.connect(str(manifest_path), timeout=10)
    conn.row_factory = sqlite3.Row

    segments = [
        r["segment"]
        for r in conn.execute(
            "SELECT segment FROM segments ORDER BY segment"
        ).fetchall()
    ]
    if not segments:
        conn.close()
        raise ValueError(f"No segments found in {manifest_path}")

    print(f"\nTournament: {source_id} ({len(segments)} segments)\n")

    groups = detect_games(segments)

    print(f"Detected {len(groups)} groups (gap threshold: 60s):\n")

    game_num = 0
    spec_groups = []

    for i, group in enumerate(groups):
        seg_names = [s for s, _, _ in group]
        start_sec = group[0][1]
        end_sec = group[-1][2]
        duration = end_sec - start_sec
        tiles, labels = _get_segment_stats(conn, seg_names)

        # Determine if this looks like a game or junk
        is_short = duration < 900  # less than 15 minutes
        action = "junk" if is_short else "game"
        if action == "game":
            game_num += 1
            new_id = f"{source_id}_G{game_num}"
        else:
            new_id = None

        prefix = f"  Group {i + 1}"
        time_range = f"{_fmt_time(start_sec)} - {_fmt_time(end_sec)}"
        info = f"({_fmt_duration(duration)}, {len(seg_names)} segment{'s' if len(seg_names) != 1 else ''}, {tiles:,} tiles, {labels:,} labels)"
        print(f"{prefix}: {time_range} {info}")

        if is_short:
            print("    WARNING: Very short -- possible non-game footage")

        if action == "game":
            print(f"    -> GAME: {new_id}")
        else:
            print("    -> JUNK (review before deleting)")

        for seg, s, e in group:
            print(f"      {seg}")

        print()

        spec_groups.append(
            {
                "new_id": new_id,
                "action": action,
                "segments": seg_names,
                "time_range": f"{_fmt_time(start_sec)} -- {_fmt_time(end_sec)}",
                "duration_sec": duration,
                "tile_count": tiles,
                "label_count": labels,
            }
        )

    conn.close()

    # Write spec file
    output_dir.mkdir(parents=True, exist_ok=True)
    spec_path = output_dir / f"{source_id}.json"
    spec = {"source": source_id, "groups": spec_groups}
    spec_path.write_text(json.dumps(spec, indent=2))
    print(f"Wrote confirmation file: {spec_path}")
    print("Edit this file to adjust game names or junk classification,")
    print("then run with --execute --spec <file>")

    return spec_path


# ---------------------------------------------------------------------------
# Phase 2: Execute -- create new game dirs, manifests, and copy packs
# ---------------------------------------------------------------------------


def execute(spec_path: Path, *, no_packs: bool = False):
    """Execute a tournament split based on a confirmed spec file.

    Creates all new game dirs alongside the source -- source is NOT modified.

    If no_packs is True, skips pack file copies and leaves pack_file paths
    pointing at the source dir. Packs can be copied later with --copy-packs.
    """
    spec = json.loads(spec_path.read_text())
    source_id = spec["source"]
    groups = spec["groups"]

    cfg = load_config()
    games_dir = Path(cfg.paths.games_dir)
    archive_packs = Path(cfg.paths.archive.tile_packs)
    registry = GameRegistry(cfg.paths.registry_db)

    source_dir = games_dir / source_id
    source_manifest_path = source_dir / "manifest.db"
    source_packs_dir = source_dir / "tile_packs"

    if not source_manifest_path.exists():
        raise FileNotFoundError(f"Source manifest not found: {source_manifest_path}")

    # Get source game info from registry
    source_game = registry.get_game(source_id)
    if not source_game:
        raise ValueError(f"Source game not found in registry: {source_id}")

    # Step 0: Back up databases
    backup_registry = Path(str(cfg.paths.registry_db) + f".pre-split-{source_id}")
    if not backup_registry.exists():
        shutil.copy2(cfg.paths.registry_db, backup_registry)
        print(f"Backed up registry: {backup_registry}")
    else:
        print(f"Registry backup already exists: {backup_registry}")

    backup_manifest = source_dir / "manifest.db.pre-split"
    if not backup_manifest.exists():
        shutil.copy2(source_manifest_path, backup_manifest)
        print(f"Backed up manifest: {backup_manifest}")
    else:
        print(f"Manifest backup already exists: {backup_manifest}")

    # Source F: archive for fallback pack reads
    source_f_packs = archive_packs / source_id

    game_groups = [g for g in groups if g["action"] == "game"]
    print(f"\nSplitting {source_id} into {len(game_groups)} games...\n")

    for group in game_groups:
        new_id = group["new_id"]
        segments = group["segments"]

        new_dir = games_dir / new_id
        new_packs_dir = new_dir / "tile_packs"
        new_f_packs = archive_packs / new_id

        if new_dir.exists():
            print(f"  SKIP {new_id} -- directory already exists")
            continue

        print(f"  Creating {new_id} ({len(segments)} segments)...")

        # Create directories
        new_dir.mkdir(parents=True, exist_ok=True)
        if not no_packs:
            new_packs_dir.mkdir(parents=True, exist_ok=True)
            new_f_packs.mkdir(parents=True, exist_ok=True)

        if no_packs:
            print("    Skipping pack copies (--no-packs)")
        else:
            # Copy packs
            for seg in segments:
                pack_name = f"{seg}.pack"
                dest = new_packs_dir / pack_name

                # Try D: first, then F:
                d_source = source_packs_dir / pack_name
                f_source = source_f_packs / pack_name

                if d_source.exists():
                    src = d_source
                elif f_source.exists():
                    src = f_source
                else:
                    print(f"    WARNING: pack not found for segment {seg}")
                    print(f"      Checked: {d_source}")
                    print(f"      Checked: {f_source}")
                    continue

                size_gb = src.stat().st_size / (1024**3)
                print(f"    Copying {pack_name} ({size_gb:.1f} GB)...")
                shutil.copy2(str(src), str(dest))

                # Archive to F:
                f_dest = new_f_packs / pack_name
                if not f_dest.exists():
                    shutil.copy2(str(dest), str(f_dest))

        # Create manifest
        gm = GameManifest(str(new_dir))
        gm.open()

        # Attach source manifest and copy filtered rows
        gm.conn.execute("ATTACH DATABASE ? AS src", (str(source_manifest_path),))

        seg_ph = ",".join("?" * len(segments))

        gm.conn.execute(
            f"INSERT INTO segments SELECT * FROM src.segments WHERE segment IN ({seg_ph})",
            segments,
        )
        gm.conn.execute(
            f"INSERT INTO frames SELECT * FROM src.frames WHERE segment IN ({seg_ph})",
            segments,
        )

        # Tiles -- rewrite pack_file paths to new dir
        old_packs_str = str(source_packs_dir).replace("\\", "/")
        new_packs_str = str(new_packs_dir).replace("\\", "/")
        gm.conn.execute(
            f"""INSERT INTO tiles (segment, frame_idx, row, col, pack_file, pack_offset, pack_size)
                SELECT segment, frame_idx, row, col,
                       REPLACE(REPLACE(pack_file, ?, ?), ?, ?),
                       pack_offset, pack_size
                FROM src.tiles WHERE segment IN ({seg_ph})""",
            [
                str(source_packs_dir),
                str(new_packs_dir),
                old_packs_str,
                new_packs_str,
            ]
            + segments,
        )

        # Labels -- filter by tile_stem prefix
        for seg in segments:
            gm.conn.execute(
                """INSERT INTO labels (tile_stem, class_id, cx, cy, w, h, source, confidence, qa_verdict)
                   SELECT tile_stem, class_id, cx, cy, w, h, source, confidence, qa_verdict
                   FROM src.labels WHERE tile_stem LIKE ? || '_frame_%'""",
                (seg,),
            )

        # Copy field_boundary metadata only
        fb = gm.conn.execute(
            "SELECT value FROM src.metadata WHERE key = 'field_boundary'"
        ).fetchone()
        if fb:
            gm.conn.execute(
                "INSERT OR IGNORE INTO metadata (key, value) VALUES ('field_boundary', ?)",
                (fb[0],),
            )

        gm.conn.commit()
        gm.conn.execute("DETACH DATABASE src")

        stats = gm.get_stats()
        gm.close()

        print(
            f"    Manifest: {stats['tiles']:,} tiles, {stats['labels']:,} labels, "
            f"{stats['segments']} segments"
        )

        # Register in registry
        registry.register_game(
            new_id,
            team=source_game["team"],
            date=source_game["date"],
            opponent=source_game.get("opponent"),
            location=source_game.get("location"),
            video_path=source_game.get("video_path"),
            needs_flip=bool(source_game.get("needs_flip")),
            game_type=source_game.get("game_type"),
            camera_type=source_game.get("camera_type", "dahua"),
            trainable=True,
            pipeline_state="LABELED",
        )

        # Update stats in registry
        registry.update_stats(
            new_id,
            tile_count=stats["tiles"],
            label_count=stats["labels"],
            positive_count=stats["positive_tiles"],
            segment_count=stats["segments"],
        )

        print(f"    Registered {new_id} as LABELED")

    registry.close()
    print(f"\nDone. Source {source_id} is untouched.")
    print(f"Run --verify --spec {spec_path} to check integrity before cleanup.")


# ---------------------------------------------------------------------------
# Phase 3: Verify -- integrity checks before cleanup
# ---------------------------------------------------------------------------


def verify(spec_path: Path) -> bool:
    """Verify split integrity. Returns True if all checks pass."""
    spec = json.loads(spec_path.read_text())
    source_id = spec["source"]
    groups = spec["groups"]

    cfg = load_config()
    games_dir = Path(cfg.paths.games_dir)
    archive_packs = Path(cfg.paths.archive.tile_packs)
    registry = GameRegistry(cfg.paths.registry_db)

    source_dir = games_dir / source_id
    source_manifest_path = source_dir / "manifest.db"

    # Get source totals
    src_conn = sqlite3.connect(str(source_manifest_path), timeout=10)
    src_total_tiles = src_conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    src_total_labels = src_conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    src_conn.close()

    game_groups = [g for g in groups if g["action"] == "game"]
    junk_groups = [g for g in groups if g["action"] == "junk"]

    # Get junk tile/label counts to subtract from source totals
    src_conn = sqlite3.connect(str(source_manifest_path), timeout=10)
    junk_segments = [s for g in junk_groups for s in g["segments"]]
    junk_tiles, junk_labels = _get_segment_stats(src_conn, junk_segments)
    src_conn.close()

    expected_tiles = src_total_tiles - junk_tiles
    expected_labels = src_total_labels - junk_labels

    errors = []
    total_tiles = 0
    total_labels = 0

    print(f"\nVerifying split of {source_id}...\n")
    print(f"  Source: {src_total_tiles:,} tiles, {src_total_labels:,} labels")
    print(f"  Junk:   {junk_tiles:,} tiles, {junk_labels:,} labels")
    print(
        f"  Expected across games: {expected_tiles:,} tiles, {expected_labels:,} labels\n"
    )

    for group in game_groups:
        new_id = group["new_id"]
        segments = group["segments"]
        new_dir = games_dir / new_id

        print(f"  Checking {new_id}...")

        # Check directory exists
        if not new_dir.exists():
            errors.append(f"{new_id}: directory does not exist")
            continue

        # Check manifest exists and has data
        gm = GameManifest(str(new_dir))
        try:
            gm.open(create=False)
        except FileNotFoundError:
            errors.append(f"{new_id}: manifest.db not found")
            continue

        stats = gm.get_stats()
        tiles = stats["tiles"]
        labels = stats["labels"]
        total_tiles += tiles
        total_labels += labels

        if tiles == 0:
            errors.append(f"{new_id}: has 0 tiles")

        print(f"    {tiles:,} tiles, {labels:,} labels, {stats['segments']} segments")

        # Check pack files exist on F: archive (D: is just a cache)
        f_packs_dir = archive_packs / new_id
        for seg in segments:
            pack_path = f_packs_dir / f"{seg}.pack"
            if not pack_path.exists():
                errors.append(f"{new_id}: missing pack on F: {seg}.pack")
            elif pack_path.stat().st_size == 0:
                errors.append(f"{new_id}: empty pack on F: {seg}.pack")

        # Check pack_file paths point to new dir
        bad_paths = gm.conn.execute(
            "SELECT DISTINCT pack_file FROM tiles WHERE pack_file NOT LIKE ?",
            (f"%{new_id}%",),
        ).fetchall()
        if bad_paths:
            errors.append(
                f"{new_id}: {len(bad_paths)} tiles have pack_file pointing outside game dir"
            )

        # Spot-check: verify a random tile's pack reference against F: archive
        sample = gm.conn.execute(
            "SELECT pack_file, pack_offset, pack_size FROM tiles WHERE pack_file IS NOT NULL LIMIT 1"
        ).fetchone()
        if sample:
            # Resolve pack on F: by extracting segment from the D: path
            pack_name = Path(sample[0]).name
            f_pack = f_packs_dir / pack_name
            if f_pack.exists():
                actual_size = f_pack.stat().st_size
                end = sample[1] + sample[2]
                if end > actual_size:
                    errors.append(
                        f"{new_id}: tile references past end of pack "
                        f"(offset+size={end}, pack_size={actual_size})"
                    )
            else:
                errors.append(f"{new_id}: spot-check pack not found on F: {f_pack}")

        # Check registry (pipeline may have advanced past LABELED)
        reg_game = registry.get_game(new_id)
        if not reg_game:
            errors.append(f"{new_id}: not found in registry")

        gm.close()

    # Check totals
    print(f"\n  Totals across games: {total_tiles:,} tiles, {total_labels:,} labels")
    print(
        f"  Expected:            {expected_tiles:,} tiles, {expected_labels:,} labels"
    )

    if total_tiles != expected_tiles:
        errors.append(
            f"Tile count mismatch: got {total_tiles:,}, expected {expected_tiles:,}"
        )
    if total_labels < expected_labels:
        errors.append(
            f"Label count too low: got {total_labels:,}, expected >= {expected_labels:,}"
        )
    elif total_labels > expected_labels:
        print(
            f"  (Pipeline added {total_labels - expected_labels:,} new labels since split)"
        )

    # Check source still exists
    source_game = registry.get_game(source_id)
    if not source_game:
        errors.append(f"Source {source_id} no longer in registry (should be untouched)")
    if not source_dir.exists():
        errors.append(f"Source directory no longer exists: {source_dir}")

    registry.close()

    if errors:
        print(f"\n  FAIL -- {len(errors)} error(s):")
        for e in errors:
            print(f"    FAIL: {e}")
        return False
    else:
        print("\n  PASS -- all checks passed")
        print(f"\n  Run --cleanup --spec {spec_path} --confirm to remove source")
        return True


# ---------------------------------------------------------------------------
# Phase 4: Cleanup -- remove source after verification
# ---------------------------------------------------------------------------


def cleanup(spec_path: Path, confirm: bool = False):
    """Remove source tournament after splits are verified."""
    if not confirm:
        print("Cleanup requires --confirm flag. This will delete:")
        print("  - Source tournament directory on D:")
        print("  - Source pack archive on F:")
        print("  - Source registry entry (marked EXCLUDED)")
        print("  - Junk segment packs from D: and F:")
        return

    spec = json.loads(spec_path.read_text())
    source_id = spec["source"]
    groups = spec["groups"]

    cfg = load_config()
    games_dir = Path(cfg.paths.games_dir)
    archive_packs = Path(cfg.paths.archive.tile_packs)
    registry = GameRegistry(cfg.paths.registry_db)

    # Verify first
    print("Running verification before cleanup...")
    if not verify(spec_path):
        print("\nVerification FAILED -- aborting cleanup")
        registry.close()
        return

    source_dir = games_dir / source_id
    source_f_dir = archive_packs / source_id

    # Delete junk packs from F:
    junk_groups = [g for g in groups if g["action"] == "junk"]
    for group in junk_groups:
        for seg in group["segments"]:
            f_pack = source_f_dir / f"{seg}.pack"
            if f_pack.exists():
                size_gb = f_pack.stat().st_size / (1024**3)
                print(f"  Deleting junk pack from F: {f_pack.name} ({size_gb:.1f} GB)")
                f_pack.unlink()

    # Delete source directory from D:
    if source_dir.exists():
        total_size = sum(f.stat().st_size for f in source_dir.rglob("*") if f.is_file())
        size_gb = total_size / (1024**3)
        print(f"  Deleting source dir from D: {source_dir} ({size_gb:.1f} GB)")
        shutil.rmtree(str(source_dir))

    # Delete source packs from F: (non-junk already copied to per-game archive dirs)
    if source_f_dir.exists():
        remaining = list(source_f_dir.iterdir())
        if remaining:
            total_size = sum(f.stat().st_size for f in remaining if f.is_file())
            size_gb = total_size / (1024**3)
            print(
                f"  Deleting source archive from F: {source_f_dir} ({size_gb:.1f} GB)"
            )
        shutil.rmtree(str(source_f_dir))

    # Mark source as EXCLUDED in registry
    source_game = registry.get_game(source_id)
    if source_game:
        registry.rename_game(source_id, f"{source_id}_SPLIT")
        registry.set_state(f"{source_id}_SPLIT", "EXCLUDED")
        print(f"  Registry: {source_id} -> {source_id}_SPLIT (EXCLUDED)")

    registry.close()
    print("\nCleanup complete.")
