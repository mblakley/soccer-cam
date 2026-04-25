"""Comprehensive data integrity audit for the training pipeline.

Cross-references every data source for every game and produces an
actionable report grouped by severity.

Usage:
    uv run python -m training.pipeline.audit
    uv run python -m training.pipeline.audit --game flash__2024.06.30_vs_IYSA_away
    uv run python -m training.pipeline.audit --force   # re-check even clean games
"""

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

TILES_PER_FRAME = 21  # 3 rows × 7 cols
FRAME_INTERVAL = 4
FPS = 30

# States where we expect real data to exist
TILED_PLUS = {
    "TILED",
    "LABELING",
    "LABELED",
    "QA_PENDING",
    "QA_DONE",
    "REVIEW_PENDING",
    "TRAINABLE",
}
LABELED_PLUS = {"LABELED", "QA_PENDING", "QA_DONE", "REVIEW_PENDING", "TRAINABLE"}
QA_DONE_PLUS = {"QA_DONE", "REVIEW_PENDING", "TRAINABLE"}


@dataclass
class AuditResult:
    level: str  # CRITICAL, WARNING, INFO
    game_id: str
    check: str
    message: str
    fix_command: str | None = None


def _video_segments(video_path: str) -> list[str]:
    """List segment stems from the video source directory on F:."""
    vp = Path(video_path)
    if not vp.exists():
        return []
    segments = []
    for f in sorted(vp.glob("*.mp4")):
        stem = f.stem
        if "[F]" in stem or "[0@0]" in stem:
            segments.append(stem)
    for f in sorted(vp.glob("*.dav")):
        stem = f.stem
        if "[F]" in stem or "[0@0]" in stem:
            segments.append(stem)
    return segments


# ------------------------------------------------------------------
# Per-game checks
# ------------------------------------------------------------------


def check_video_source(game: dict, cfg) -> list[AuditResult]:
    """Check 1: Video source exists and has segment files."""
    results = []
    gid = game["game_id"]
    vpath = game.get("video_path", "")

    if not vpath:
        results.append(
            AuditResult("WARNING", gid, "video_source", "No video_path in registry")
        )
        return results

    if not Path(vpath).exists():
        results.append(
            AuditResult(
                "WARNING", gid, "video_source", f"Video path not found: {vpath}"
            )
        )
        return results

    segs = _video_segments(vpath)
    if not segs:
        results.append(
            AuditResult("WARNING", gid, "video_source", f"No segment videos in {vpath}")
        )

    return results


def check_manifest_segments(game: dict, cfg) -> list[AuditResult]:
    """Check 2: Manifest segments match video source segments."""
    results = []
    gid = game["game_id"]
    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"

    if not manifest_path.exists():
        if game["pipeline_state"] in TILED_PLUS:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "manifest_missing",
                    "No manifest.db but state is " + game["pipeline_state"],
                )
            )
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)
    manifest_segs = set(gm.get_segments())
    gm.close()

    video_segs = set(_video_segments(game.get("video_path", "")))

    if video_segs and manifest_segs:
        missing = video_segs - manifest_segs
        if missing:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "manifest_segments",
                    f"Manifest has {len(manifest_segs)} segments but {len(video_segs)} video segments exist. "
                    f"Missing: {', '.join(sorted(s[:30] for s in missing))}",
                    fix_command=f"uv run python -m training.pipeline enqueue tile --game {gid} --priority 10",
                )
            )

    return results


def check_tile_integrity(game: dict, cfg) -> list[AuditResult]:
    """Check 3: Tile counts, frame completeness, pack files on F:."""
    results = []
    gid = game["game_id"]
    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"

    if not manifest_path.exists():
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)

    try:
        actual_tiles = gm.get_tile_count()
        segments = gm.get_segment_summary()

        # Registry vs manifest tile count
        reg_tiles = game.get("tile_count", 0)
        if reg_tiles != actual_tiles:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "tile_count_mismatch",
                    f"Registry tile_count={reg_tiles} but manifest has {actual_tiles}",
                )
            )

        # Registry vs manifest segment count
        reg_segs = game.get("segment_count", 0)
        if reg_segs != len(segments):
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "segment_count_mismatch",
                    f"Registry segment_count={reg_segs} but manifest has {len(segments)}",
                )
            )

        # Frame completeness: every frame should have 21 tiles
        incomplete = gm.conn.execute(
            "SELECT segment, frame_idx, COUNT(*) as cnt FROM tiles "
            "GROUP BY segment, frame_idx HAVING cnt != 21 LIMIT 10"
        ).fetchall()
        if incomplete:
            examples = [f"{r[0][:20]}..f{r[1]}({r[2]}tiles)" for r in incomplete[:3]]
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "incomplete_frames",
                    f"{len(incomplete)}+ frames with != 21 tiles: {', '.join(examples)}",
                )
            )

        # Pack files on F: (source of truth)
        archive_dir = Path(cfg.paths.archive.tile_packs) / gid
        packs_in_manifest = set()
        rows = gm.conn.execute(
            "SELECT DISTINCT pack_file FROM tiles WHERE pack_file IS NOT NULL"
        ).fetchall()
        for r in rows:
            packs_in_manifest.add(Path(r[0]).name)

        for pack_name in packs_in_manifest:
            f_path = archive_dir / pack_name
            if not f_path.exists():
                # Check D: as fallback
                d_path = games_dir / gid / "tile_packs" / pack_name
                if d_path.exists():
                    results.append(
                        AuditResult(
                            "CRITICAL",
                            gid,
                            "pack_not_archived",
                            f"Pack {pack_name} exists on D: but NOT on F: (not archived!)",
                        )
                    )
                else:
                    results.append(
                        AuditResult(
                            "CRITICAL",
                            gid,
                            "pack_missing",
                            f"Pack {pack_name} referenced by manifest but missing from both D: and F:",
                        )
                    )

        # Spot-check: verify a tile from each segment is valid JPEG
        for seg_info in segments:
            seg = seg_info["segment"]
            # Get first tile in this segment
            tile = gm.conn.execute(
                "SELECT pack_file, pack_offset, pack_size FROM tiles WHERE segment=? LIMIT 1",
                (seg,),
            ).fetchone()
            if not tile or not tile[0]:
                continue
            pack_name = Path(tile[0]).name
            pack_path = archive_dir / pack_name
            if not pack_path.exists():
                pack_path = games_dir / gid / "tile_packs" / pack_name
            if pack_path.exists():
                try:
                    with open(pack_path, "rb") as f:
                        f.seek(tile[1])
                        header = f.read(2)
                        if header != b"\xff\xd8":
                            results.append(
                                AuditResult(
                                    "CRITICAL",
                                    gid,
                                    "corrupt_tile",
                                    f"Tile in {seg[:25]} is not valid JPEG (header: {header.hex()})",
                                )
                            )
                except OSError as e:
                    results.append(
                        AuditResult(
                            "WARNING",
                            gid,
                            "pack_read_error",
                            f"Cannot read {pack_name}: {e}",
                        )
                    )

        # Orphaned packs on F: with no manifest segment
        if archive_dir.exists():
            f_packs = {p.name for p in archive_dir.glob("*.pack")}
            orphaned = f_packs - packs_in_manifest
            if orphaned:
                results.append(
                    AuditResult(
                        "WARNING",
                        gid,
                        "orphaned_packs",
                        f"{len(orphaned)} pack(s) on F: with no manifest entry: {', '.join(sorted(s[:25] for s in orphaned))}",
                    )
                )

    finally:
        gm.close()

    return results


def check_label_integrity(game: dict, cfg) -> list[AuditResult]:
    """Check 4: Label counts, per-segment distribution, coordinate bounds."""
    results = []
    gid = game["game_id"]

    if game["pipeline_state"] not in LABELED_PLUS:
        return results

    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"
    if not manifest_path.exists():
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)

    try:
        actual_labels = gm.get_label_count()
        actual_positive = gm.get_positive_tile_count()
        actual_tiles = gm.get_tile_count()

        # Registry vs manifest
        reg_labels = game.get("label_count", 0)
        if reg_labels != actual_labels:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "label_count_mismatch",
                    f"Registry label_count={reg_labels} but manifest has {actual_labels}",
                )
            )

        reg_positive = game.get("positive_count", 0)
        if reg_positive != actual_positive:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "positive_count_mismatch",
                    f"Registry positive_count={reg_positive} but manifest has {actual_positive}",
                )
            )

        # Logical constraints
        if actual_labels == 0:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "no_labels",
                    f"State is {game['pipeline_state']} but manifest has 0 labels",
                    fix_command=f"uv run python -m training.pipeline enqueue label --game {gid} --priority 10",
                )
            )
        elif actual_positive > actual_labels:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "positive_exceeds_labels",
                    f"positive={actual_positive} > labels={actual_labels}",
                )
            )

        # Coverage check
        if actual_tiles > 0:
            actual_coverage = actual_positive / actual_tiles
            reg_coverage = game.get("coverage", 0.0)
            if abs(actual_coverage - reg_coverage) > 0.01:
                results.append(
                    AuditResult(
                        "WARNING",
                        gid,
                        "coverage_mismatch",
                        f"Registry coverage={reg_coverage:.3f} but actual={actual_coverage:.3f}",
                    )
                )

        # Per-segment label distribution
        segments = gm.get_segment_summary()
        if segments and actual_labels > 0:
            for seg_info in segments:
                seg = seg_info["segment"]
                seg_labels = gm.conn.execute(
                    "SELECT COUNT(*) FROM labels WHERE tile_stem LIKE ?",
                    (f"{seg}%",),
                ).fetchone()[0]
                if seg_labels == 0:
                    results.append(
                        AuditResult(
                            "CRITICAL",
                            gid,
                            "segment_no_labels",
                            f"Segment {seg[:30]} has 0 labels (was labeling skipped for this segment?)",
                            fix_command=f"uv run python -m training.pipeline enqueue label --game {gid} --priority 10",
                        )
                    )

        # Coordinate bounds
        oob = gm.conn.execute(
            "SELECT COUNT(*) FROM labels WHERE cx < 0 OR cx > 1 OR cy < 0 OR cy > 1 "
            "OR w < 0 OR w > 1 OR h < 0 OR h > 1"
        ).fetchone()[0]
        if oob > 0:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "label_oob",
                    f"{oob} labels with coordinates outside [0, 1]",
                )
            )

    finally:
        gm.close()

    return results


def check_qa_integrity(game: dict, cfg) -> list[AuditResult]:
    """Check 5: QA metadata for QA_DONE+ games."""
    results = []
    gid = game["game_id"]

    if game["pipeline_state"] not in QA_DONE_PLUS:
        return results

    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"
    if not manifest_path.exists():
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)

    try:
        # Ball track metadata
        ball_track = gm.get_metadata("game_ball_track")
        if not ball_track:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "no_ball_track",
                    "State is QA_DONE+ but no game_ball_track metadata",
                )
            )

        # Field boundary
        field_boundary = gm.get_metadata("field_boundary")
        if not field_boundary:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "no_field_boundary",
                    "No field_boundary metadata (needed for field mask filtering)",
                )
            )

        # Game phases
        phases = gm.get_phases()
        if not phases:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "no_phases",
                    "No game phases detected (needed for phase filtering)",
                )
            )

        # QA verdicts on labels
        verdict_count = gm.conn.execute(
            "SELECT COUNT(*) FROM labels WHERE qa_verdict IS NOT NULL"
        ).fetchone()[0]
        total_labels = gm.get_label_count()
        if total_labels > 0 and verdict_count == 0:
            results.append(
                AuditResult(
                    "WARNING",
                    gid,
                    "no_qa_verdicts",
                    f"State is QA_DONE+ but 0/{total_labels} labels have qa_verdict",
                )
            )

    finally:
        gm.close()

    return results


def check_state_consistency(game: dict, cfg) -> list[AuditResult]:
    """Check 6: Pipeline state matches actual data."""
    results = []
    gid = game["game_id"]
    state = game["pipeline_state"]

    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"

    if state in TILED_PLUS and not manifest_path.exists():
        results.append(
            AuditResult(
                "CRITICAL",
                gid,
                "state_no_manifest",
                f"State={state} but no manifest.db",
            )
        )
        return results

    if state not in TILED_PLUS:
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)

    try:
        tiles = gm.get_tile_count()
        labels = gm.get_label_count()
        segments = len(gm.get_segments())

        if state in TILED_PLUS and tiles == 0:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "state_no_tiles",
                    f"State={state} but 0 tiles in manifest",
                )
            )

        if state in TILED_PLUS and segments == 0:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "state_no_segments",
                    f"State={state} but 0 segments in manifest",
                )
            )

        if state in LABELED_PLUS and labels == 0:
            results.append(
                AuditResult(
                    "CRITICAL",
                    gid,
                    "state_no_labels",
                    f"State={state} but 0 labels in manifest",
                    fix_command=f"uv run python -m training.pipeline enqueue label --game {gid} --priority 10",
                )
            )

    finally:
        gm.close()

    # Attempt counter
    attempts = game.get("pipeline_attempts", 0)
    if attempts > 3:
        results.append(
            AuditResult(
                "WARNING",
                gid,
                "high_attempts",
                f"pipeline_attempts={attempts} — possible retry loop",
            )
        )

    return results


def check_pack_archive(game: dict, cfg) -> list[AuditResult]:
    """Check 7: Pack archive consistency (F: is source of truth)."""
    results = []
    gid = game["game_id"]
    games_dir = Path(cfg.paths.games_dir)
    manifest_path = games_dir / gid / "manifest.db"

    if not manifest_path.exists():
        return results

    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest(games_dir / gid)
    gm.open(create=False)

    try:
        # Get max offset+size per pack file from manifest
        pack_bounds = {}
        rows = gm.conn.execute(
            "SELECT pack_file, MAX(pack_offset + pack_size) as max_end "
            "FROM tiles WHERE pack_file IS NOT NULL GROUP BY pack_file"
        ).fetchall()
        for r in rows:
            pack_name = Path(r[0]).name
            pack_bounds[pack_name] = r[1]

        archive_dir = Path(cfg.paths.archive.tile_packs) / gid

        for pack_name, expected_min_size in pack_bounds.items():
            f_path = archive_dir / pack_name
            if f_path.exists():
                actual_size = f_path.stat().st_size
                if actual_size < expected_min_size:
                    results.append(
                        AuditResult(
                            "CRITICAL",
                            gid,
                            "pack_truncated",
                            f"Pack {pack_name} on F: is {actual_size} bytes but manifest references up to {expected_min_size}",
                        )
                    )

    finally:
        gm.close()

    return results


# ------------------------------------------------------------------
# System-wide checks
# ------------------------------------------------------------------


def audit_system() -> list[AuditResult]:
    """Check 8: Queue health, worker status, duplicate items."""
    from training.pipeline.client import PipelineClient

    results = []
    client = PipelineClient()

    # Dead tasks — permanently failed, need manual intervention
    now = time.time()
    dead = client.get_queue_items(status="dead")
    for item in dead:
        results.append(
            AuditResult(
                "CRITICAL",
                item.get("game_id", "?"),
                "dead_task",
                f"Task {item['id']} ({item['task_type']}) is DEAD after repeated failures: {(item.get('error') or '')[:60]}",
                fix_command=f"uv run python -m training.pipeline enqueue {item['task_type']} --game {item.get('game_id', '?')} --priority 10",
            )
        )

    # Stale running items (no heartbeat in 2 hours)
    running = client.get_queue_items(status="running")
    for item in running:
        hb = item.get("heartbeat_at") or item.get("started_at") or 0
        if hb and (now - hb) > 7200:
            elapsed_min = int((now - hb) / 60)
            results.append(
                AuditResult(
                    "WARNING",
                    item.get("game_id", "?"),
                    "stale_running_task",
                    f"Task {item['id']} ({item['task_type']}) running with no heartbeat for {elapsed_min} min",
                )
            )

    # Duplicate queue items (same game_id + task_type in queued/running)
    active = client.get_queue_items(status="queued") + running
    seen = {}
    for item in active:
        key = (item["game_id"], item["task_type"])
        if key in seen:
            results.append(
                AuditResult(
                    "WARNING",
                    item["game_id"],
                    "duplicate_queue_item",
                    f"Duplicate {item['task_type']} items: #{seen[key]} and #{item['id']}",
                )
            )
        else:
            seen[key] = item["id"]

    # Worker health
    try:
        status = client.get_status()
        workers = status.get("workers", [])
        for w in workers:
            last_seen = w.get("last_seen", 0)
            if last_seen and (now - last_seen) > 3600:
                elapsed_min = int((now - last_seen) / 60)
                results.append(
                    AuditResult(
                        "WARNING",
                        "",
                        "stale_worker",
                        f"Worker {w.get('hostname', '?')} last seen {elapsed_min} min ago",
                    )
                )
    except Exception:
        pass

    return results


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Pipeline data integrity audit")
    parser.add_argument("--game", help="Audit a single game")
    parser.add_argument(
        "--force", action="store_true", help="Re-check even clean games"
    )
    args = parser.parse_args()

    from training.pipeline.client import PipelineClient
    from training.pipeline.config import load_config

    cfg = load_config()
    client = PipelineClient()

    all_games = client.get_all_games()
    games_dir = Path(cfg.paths.games_dir)

    # Filter to specific game if requested
    if args.game:
        all_games = [g for g in all_games if g["game_id"] == args.game]
        if not all_games:
            logger.error("Game not found: %s", args.game)
            return

    # Only audit games that are at least TILED (or have manifest.db)
    auditable = [g for g in all_games if g["pipeline_state"] in TILED_PLUS]
    skipped_states = len(all_games) - len(auditable)

    all_results: list[AuditResult] = []
    clean_count = 0
    skipped_clean = 0

    for game in auditable:
        gid = game["game_id"]
        manifest_path = games_dir / gid / "manifest.db"

        # Incremental: skip clean games that haven't changed
        if not args.force and manifest_path.exists():
            from training.data_prep.game_manifest import GameManifest

            gm = GameManifest(games_dir / gid)
            gm.open(create=False)
            audit_ts = gm.get_metadata("audit_passed")
            gm.close()
            if audit_ts and float(audit_ts) > game.get("pipeline_updated", 0):
                skipped_clean += 1
                continue

        logger.info("Auditing %s (%s)...", gid, game["pipeline_state"])

        game_results = []
        game_results += check_video_source(game, cfg)
        game_results += check_manifest_segments(game, cfg)
        game_results += check_tile_integrity(game, cfg)
        game_results += check_label_integrity(game, cfg)
        game_results += check_qa_integrity(game, cfg)
        game_results += check_state_consistency(game, cfg)
        game_results += check_pack_archive(game, cfg)

        issues = [r for r in game_results if r.level != "INFO"]
        if not issues:
            clean_count += 1
            # Stamp as clean
            if manifest_path.exists():
                from training.data_prep.game_manifest import GameManifest

                gm = GameManifest(games_dir / gid)
                gm.open(create=False)
                gm.set_metadata("audit_passed", str(time.time()))
                gm.close()

        all_results += game_results

    # System-wide checks
    logger.info("Running system-wide checks...")
    all_results += audit_system()

    # Print report
    print()
    print("=" * 60)
    print("PIPELINE DATA INTEGRITY AUDIT")
    print("=" * 60)
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Games audited: {len(auditable) - skipped_clean}")
    print(f"  Skipped (not tiled): {skipped_states}")
    print(f"  Skipped (already clean): {skipped_clean}")
    print()

    # Group by severity
    critical = [r for r in all_results if r.level == "CRITICAL"]
    warnings = [r for r in all_results if r.level == "WARNING"]

    if critical:
        print(f"CRITICAL ({len(critical)}):")
        for r in critical:
            print(f"  {r.game_id}:")
            print(f"    [{r.check}] {r.message}")
            if r.fix_command:
                print(f"    -> Fix: {r.fix_command}")
        print()

    if warnings:
        print(f"WARNING ({len(warnings)}):")
        for r in warnings:
            prefix = f"  {r.game_id}: " if r.game_id else "  "
            print(f"{prefix}[{r.check}] {r.message}")
            if r.fix_command:
                print(f"    -> Fix: {r.fix_command}")
        print()

    print(f"CLEAN: {clean_count + skipped_clean} games passed all checks")

    if critical:
        print(f"\n*** {len(critical)} CRITICAL issue(s) need attention ***")


if __name__ == "__main__":
    main()
