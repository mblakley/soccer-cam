"""Verify tile completeness and integrity for all games.

Checks:
1. Zip integrity (F:/tile_zips/) — testzip() for corruption
2. Tile directory completeness (D:/tiles_640/) — all segments, 21 tiles/frame
3. Cross-reference against game_registry.json
4. Reports gaps that need re-tiling

Usage:
    python -u training/data_prep/verify_tiles.py [--tiles-dir D:/training_data/tiles_640]
                                                  [--zips-dir F:/tile_zips]
                                                  [--registry D:/training_data/game_registry.json]
                                                  [--fix]  # re-tile gaps (not yet implemented)
"""
import argparse
import json
import logging
import os
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("verify_tiles.log"),
    ],
)
logger = logging.getLogger()

EXPECTED_ROWS = 3
EXPECTED_COLS = 7
TILES_PER_FRAME = EXPECTED_ROWS * EXPECTED_COLS  # 21

# Regex to parse tile filenames:
#   {segment_stem}_frame_{NNNNNN}_r{R}_c{C}.jpg
# segment_stem can contain brackets, dots, dashes, underscores — everything up to _frame_
TILE_RE = re.compile(
    r"^(?P<segment>.+)_frame_(?P<frame>\d{6})_r(?P<row>\d+)_c(?P<col>\d+)\.jpg$"
)


@dataclass
class SegmentReport:
    segment_id: str  # stem of the .mp4 filename
    frame_count: int = 0
    tile_count: int = 0
    incomplete_frames: list = field(default_factory=list)  # frames with <21 tiles
    missing_tiles: list = field(default_factory=list)  # (frame, row, col) tuples
    frame_indices: list = field(default_factory=list)  # sorted list of frame numbers
    max_gap: int = 0  # largest gap between consecutive frames (in frame indices)


@dataclass
class GameReport:
    game_id: str
    source: str = ""  # "tiles_dir" or "zip"
    segments_expected: list = field(default_factory=list)
    segments_found: list = field(default_factory=list)
    segments_missing: list = field(default_factory=list)
    segment_reports: dict = field(default_factory=dict)  # segment_id -> SegmentReport
    total_frames: int = 0
    total_tiles: int = 0
    issues: list = field(default_factory=list)
    zip_corrupt: bool = False
    zip_bad_file: str = ""


def parse_tile_name(name: str) -> dict | None:
    """Parse a tile filename into its components."""
    m = TILE_RE.match(name)
    if not m:
        return None
    return {
        "segment": m.group("segment"),
        "frame": int(m.group("frame")),
        "row": int(m.group("row")),
        "col": int(m.group("col")),
    }


def segment_stem(mp4_name: str) -> str:
    """Get the stem used in tile filenames from an mp4 filename."""
    return mp4_name.replace(".mp4", "")


def analyze_tiles(tile_names: list[str], game_id: str) -> dict[str, SegmentReport]:
    """Analyze a list of tile filenames and return per-segment reports."""
    # Group tiles by segment and frame
    # seg -> frame -> set of (row, col)
    seg_frames = defaultdict(lambda: defaultdict(set))
    unparsed = []

    for name in tile_names:
        # Strip game_id prefix if present (zips store as game_id/filename)
        if name.startswith(game_id + "/"):
            name = name[len(game_id) + 1:]

        parsed = parse_tile_name(name)
        if parsed is None:
            unparsed.append(name)
            continue
        seg_frames[parsed["segment"]][parsed["frame"]].add(
            (parsed["row"], parsed["col"])
        )

    if unparsed:
        logger.warning("  %d unparseable filenames (first 3: %s)", len(unparsed), unparsed[:3])

    reports = {}
    for seg_id in sorted(seg_frames.keys()):
        frames = seg_frames[seg_id]
        report = SegmentReport(segment_id=seg_id)
        report.frame_count = len(frames)
        report.frame_indices = sorted(frames.keys())

        # Check for large gaps
        if len(report.frame_indices) > 1:
            gaps = [
                report.frame_indices[i + 1] - report.frame_indices[i]
                for i in range(len(report.frame_indices) - 1)
            ]
            report.max_gap = max(gaps)

        # Check tile completeness per frame
        expected_tiles = {(r, c) for r in range(EXPECTED_ROWS) for c in range(EXPECTED_COLS)}
        for frame_idx in sorted(frames.keys()):
            tiles = frames[frame_idx]
            report.tile_count += len(tiles)
            if tiles != expected_tiles:
                missing = expected_tiles - tiles
                extra = tiles - expected_tiles
                report.incomplete_frames.append(frame_idx)
                for r, c in missing:
                    report.missing_tiles.append((frame_idx, r, c))
                if extra:
                    logger.warning(
                        "  %s frame %d: unexpected tiles %s",
                        seg_id, frame_idx, extra,
                    )

        reports[seg_id] = report

    return reports


def verify_tile_directory(tiles_dir: Path, game_id: str, expected_segments: list[str]) -> GameReport:
    """Verify a tile directory on disk."""
    report = GameReport(game_id=game_id, source="tiles_dir")
    report.segments_expected = [segment_stem(s) for s in expected_segments]

    game_dir = tiles_dir / game_id
    if not game_dir.exists():
        report.issues.append("MISSING: tile directory does not exist")
        report.segments_missing = report.segments_expected[:]
        return report

    # List all files — use os.listdir for speed on large directories (90K+ files)
    logger.info("  Scanning %s...", game_id)
    tile_names = [f for f in os.listdir(game_dir) if f.endswith(".jpg")]
    logger.info("  %s: %d tile files found", game_id, len(tile_names))
    if not tile_names:
        report.issues.append("EMPTY: tile directory exists but has no .jpg files")
        report.segments_missing = report.segments_expected[:]
        return report

    # Analyze tiles
    seg_reports = analyze_tiles(tile_names, game_id)
    report.segment_reports = seg_reports
    report.segments_found = list(seg_reports.keys())

    # Check segment coverage
    expected_stems = set(report.segments_expected)
    found_stems = set(report.segments_found)
    report.segments_missing = sorted(expected_stems - found_stems)

    # Extra segments (tiled but not in registry — could be from a raw zip or renamed source)
    extra = found_stems - expected_stems
    if extra:
        # Try fuzzy matching — sometimes the registry has the full name but tiling used a shorter form
        unmatched_extra = set()
        for e in extra:
            matched = False
            for exp in expected_stems:
                if e in exp or exp in e:
                    matched = True
                    break
            if not matched:
                unmatched_extra.add(e)

        # Raw-zip pattern: game was tiled from a single combined video file instead of
        # individual camera segments. All expected segment data is present in one tile stem.
        # If we have 1 unmatched extra stem and ALL expected stems are missing, this is a
        # raw-zip game — the data is complete, just named differently.
        if len(unmatched_extra) == 1 and len(report.segments_missing) == len(expected_stems):
            raw_stem = list(unmatched_extra)[0]
            raw_report = seg_reports[raw_stem]
            logger.info("    (raw-zip game: all tiles under stem '%s')", raw_stem)
            report.segments_missing = []  # Not actually missing
        elif unmatched_extra:
            report.issues.append(
                f"EXTRA: {len(unmatched_extra)} segment(s) not in registry: {sorted(unmatched_extra)[:3]}"
            )

    if report.segments_missing:
        report.issues.append(
            f"MISSING_SEGMENTS: {len(report.segments_missing)} of {len(expected_stems)} "
            f"segments not found: {report.segments_missing[:3]}{'...' if len(report.segments_missing) > 3 else ''}"
        )

    # Aggregate stats
    for sr in seg_reports.values():
        report.total_frames += sr.frame_count
        report.total_tiles += sr.tile_count
        if sr.incomplete_frames:
            report.issues.append(
                f"INCOMPLETE_FRAMES: {sr.segment_id} has {len(sr.incomplete_frames)} "
                f"frames with <{TILES_PER_FRAME} tiles"
            )
        if sr.max_gap > 100:
            report.issues.append(
                f"LARGE_GAP: {sr.segment_id} has a gap of {sr.max_gap} frames "
                f"(frame range {sr.frame_indices[0]}-{sr.frame_indices[-1]})"
            )

    # Sanity check: typical game ~15-20 min per segment at 25fps/4 = ~5600 frames/seg after diff
    # But diff filtering removes a lot, so 500-3000 frames/seg is reasonable
    for sr in seg_reports.values():
        if sr.frame_count < 10:
            report.issues.append(
                f"SUSPICIOUSLY_LOW: {sr.segment_id} only has {sr.frame_count} frames"
            )

    return report


def verify_zip(zip_path: Path, game_id: str, expected_segments: list[str]) -> GameReport:
    """Verify a zip file's integrity and contents."""
    report = GameReport(game_id=game_id, source=f"zip:{zip_path.name}")
    report.segments_expected = [segment_stem(s) for s in expected_segments]

    if not zip_path.exists():
        report.issues.append(f"MISSING: zip file {zip_path} does not exist")
        return report

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Integrity check
            bad = zf.testzip()
            if bad is not None:
                report.zip_corrupt = True
                report.zip_bad_file = bad
                report.issues.append(f"CORRUPT: first bad file in zip: {bad}")
                return report

            # Catalog contents
            names = zf.namelist()
            if not names:
                report.issues.append("EMPTY: zip has no files")
                return report

            # Filter to .jpg only
            tile_names = [n for n in names if n.endswith(".jpg")]
            seg_reports = analyze_tiles(tile_names, game_id)
            report.segment_reports = seg_reports
            report.segments_found = list(seg_reports.keys())

    except zipfile.BadZipFile as e:
        report.zip_corrupt = True
        report.issues.append(f"BAD_ZIP: cannot open: {e}")
        return report
    except Exception as e:
        report.issues.append(f"ERROR: {type(e).__name__}: {e}")
        return report

    # Check segment coverage
    expected_stems = set(report.segments_expected)
    found_stems = set(report.segments_found)
    report.segments_missing = sorted(expected_stems - found_stems)

    if report.segments_missing:
        report.issues.append(
            f"MISSING_SEGMENTS: {len(report.segments_missing)} of {len(expected_stems)} "
            f"segments not found"
        )

    # Aggregate
    for sr in seg_reports.values():
        report.total_frames += sr.frame_count
        report.total_tiles += sr.tile_count
        if sr.incomplete_frames:
            report.issues.append(
                f"INCOMPLETE_FRAMES: {sr.segment_id} has {len(sr.incomplete_frames)} "
                f"frames with <{TILES_PER_FRAME} tiles"
            )
        if sr.frame_count < 10:
            report.issues.append(
                f"SUSPICIOUSLY_LOW: {sr.segment_id} only has {sr.frame_count} frames"
            )

    return report


def find_zips_for_game(zips_dir: Path, game_id: str) -> list[Path]:
    """Find all zip files belonging to a game."""
    if not zips_dir.exists():
        return []
    matches = []
    for zf in sorted(zips_dir.iterdir()):
        if zf.suffix == ".zip" and zf.name.startswith(game_id):
            matches.append(zf)
    return matches


def print_report(report: GameReport, verbose: bool = False):
    """Print a game verification report."""
    status = "OK" if not report.issues else "ISSUES"
    seg_info = f"{len(report.segments_found)}/{len(report.segments_expected)} segs"
    logger.info(
        "  [%s] %s: %s, %d frames, %d tiles (%s)",
        status, report.game_id, seg_info,
        report.total_frames, report.total_tiles, report.source,
    )

    if report.issues:
        for issue in report.issues:
            logger.info("    ! %s", issue)

    if verbose:
        for seg_id, sr in sorted(report.segment_reports.items()):
            frame_range = (
                f"frames {sr.frame_indices[0]}-{sr.frame_indices[-1]}"
                if sr.frame_indices else "no frames"
            )
            logger.info(
                "    seg %s: %d frames, %d tiles, max_gap=%d (%s)",
                seg_id, sr.frame_count, sr.tile_count, sr.max_gap, frame_range,
            )


def print_gap_summary(all_reports: list[GameReport], registry: dict):
    """Print a summary of all gaps that need filling."""
    gaps = []

    for report in all_reports:
        if not report.issues:
            continue

        for issue in report.issues:
            if issue.startswith("MISSING:") or issue.startswith("EMPTY:"):
                gaps.append({
                    "game_id": report.game_id,
                    "type": "full_game",
                    "detail": "entire game needs tiling",
                    "segments": report.segments_expected,
                })
                break
            elif issue.startswith("MISSING_SEGMENTS:"):
                gaps.append({
                    "game_id": report.game_id,
                    "type": "missing_segments",
                    "detail": f"segments: {report.segments_missing}",
                    "segments": report.segments_missing,
                })
            elif issue.startswith("CORRUPT") or issue.startswith("BAD_ZIP"):
                gaps.append({
                    "game_id": report.game_id,
                    "type": "corrupt_zip",
                    "detail": issue,
                    "segments": report.segments_expected,
                })
                break
            elif issue.startswith("SUSPICIOUSLY_LOW:"):
                seg_id = issue.split(":")[1].strip().split(" ")[0]
                gaps.append({
                    "game_id": report.game_id,
                    "type": "truncated_segment",
                    "detail": issue,
                    "segments": [seg_id],
                })
            elif issue.startswith("INCOMPLETE_FRAMES:"):
                seg_id = issue.split(":")[1].strip().split(" ")[0]
                gaps.append({
                    "game_id": report.game_id,
                    "type": "incomplete_frames",
                    "detail": issue,
                    "segments": [seg_id],
                })

    if not gaps:
        logger.info("\n=== NO GAPS FOUND — all tiles verified ===")
        return gaps

    logger.info("\n=== GAP SUMMARY: %d issues across %d games ===",
                len(gaps), len(set(g["game_id"] for g in gaps)))
    logger.info("")

    by_type = defaultdict(list)
    for g in gaps:
        by_type[g["type"]].append(g)

    for gap_type in ["full_game", "corrupt_zip", "missing_segments", "truncated_segment", "incomplete_frames"]:
        items = by_type.get(gap_type, [])
        if not items:
            continue
        logger.info("  %s (%d):", gap_type.upper(), len(items))
        for item in items:
            logger.info("    %s — %s", item["game_id"], item["detail"])
        logger.info("")

    # Write gaps to JSON for the gap-filler to consume
    gaps_file = Path("tile_gaps.json")
    with open(gaps_file, "w") as f:
        json.dump(gaps, f, indent=2)
    logger.info("Gaps written to %s", gaps_file)

    return gaps


def main():
    parser = argparse.ArgumentParser(description="Verify tile completeness")
    parser.add_argument("--tiles-dir", default="D:/training_data/tiles_640",
                        help="Directory containing extracted tile subdirectories")
    parser.add_argument("--zips-dir", default="F:/tile_zips",
                        help="Directory containing tile zip archives")
    parser.add_argument("--registry", default="D:/training_data/game_registry.json",
                        help="Path to game_registry.json")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-segment detail")
    parser.add_argument("--game", help="Verify a single game only")
    args = parser.parse_args()

    tiles_dir = Path(args.tiles_dir)
    zips_dir = Path(args.zips_dir)

    # Load registry
    with open(args.registry) as f:
        registry_list = json.load(f)

    registry = {}
    for g in registry_list:
        gid = g["game_id"]
        if gid in registry:
            # Duplicate game_id — merge segments
            registry[gid]["segments"].extend(g.get("segments", []))
        else:
            registry[gid] = g

    if args.game:
        if args.game not in registry:
            logger.error("Game %s not in registry", args.game)
            sys.exit(1)
        games_to_check = {args.game: registry[args.game]}
    else:
        games_to_check = registry

    logger.info("=== TILE VERIFICATION ===")
    logger.info("Tiles dir: %s", tiles_dir)
    logger.info("Zips dir:  %s", zips_dir)
    logger.info("Games:     %d", len(games_to_check))
    logger.info("")

    all_reports = []
    total_ok = 0
    total_issues = 0

    # Phase 1: Verify tile directories on disk
    logger.info("--- Phase 1: Verify tile directories on D: ---")
    games_with_tiles = set()
    for gid in sorted(games_to_check):
        game_dir = tiles_dir / gid
        if not game_dir.exists():
            continue
        games_with_tiles.add(gid)
        game = games_to_check[gid]
        expected_segs = game.get("segments", [])
        report = verify_tile_directory(tiles_dir, gid, expected_segs)
        all_reports.append(report)
        print_report(report, verbose=args.verbose)
        if report.issues:
            total_issues += 1
        else:
            total_ok += 1

    logger.info("")
    logger.info("Tiles on disk: %d OK, %d with issues", total_ok, total_issues)
    logger.info("")

    # Phase 2: Verify zips
    logger.info("--- Phase 2: Verify tile zips on F: ---")
    zip_ok = 0
    zip_issues = 0
    games_in_zips = set()

    if zips_dir.exists():
        # Group zips by game
        zip_map = defaultdict(list)  # game_id -> [zip_paths]
        for zf in sorted(zips_dir.iterdir()):
            if zf.suffix != ".zip":
                continue
            # Match zip name to a game_id
            for gid in games_to_check:
                if zf.name.startswith(gid):
                    zip_map[gid].append(zf)
                    break

        for gid in sorted(zip_map.keys()):
            if args.game and gid != args.game:
                continue
            games_in_zips.add(gid)
            game = games_to_check[gid]
            expected_segs = game.get("segments", [])
            zip_paths = zip_map[gid]

            if len(zip_paths) == 1:
                # Single zip for game
                report = verify_zip(zip_paths[0], gid, expected_segs)
            else:
                # Multiple segment zips — verify each, then combine
                combined = GameReport(game_id=gid, source=f"zips:{len(zip_paths)}")
                combined.segments_expected = [segment_stem(s) for s in expected_segs]
                all_seg_reports = {}
                any_corrupt = False

                for zp in zip_paths:
                    zr = verify_zip(zp, gid, expected_segs)
                    if zr.zip_corrupt:
                        any_corrupt = True
                        combined.issues.append(f"CORRUPT_ZIP: {zp.name}")
                    all_seg_reports.update(zr.segment_reports)
                    combined.total_frames += zr.total_frames
                    combined.total_tiles += zr.total_tiles

                combined.segment_reports = all_seg_reports
                combined.segments_found = list(all_seg_reports.keys())
                combined.zip_corrupt = any_corrupt

                # Check segment coverage across all zips
                expected_stems = set(combined.segments_expected)
                found_stems = set(combined.segments_found)
                combined.segments_missing = sorted(expected_stems - found_stems)
                if combined.segments_missing:
                    combined.issues.append(
                        f"MISSING_SEGMENTS: {len(combined.segments_missing)} of "
                        f"{len(expected_stems)} segments not found across {len(zip_paths)} zips"
                    )

                # Propagate per-segment issues
                for sr in all_seg_reports.values():
                    if sr.incomplete_frames:
                        combined.issues.append(
                            f"INCOMPLETE_FRAMES: {sr.segment_id} has "
                            f"{len(sr.incomplete_frames)} frames with <{TILES_PER_FRAME} tiles"
                        )
                    if sr.frame_count < 10:
                        combined.issues.append(
                            f"SUSPICIOUSLY_LOW: {sr.segment_id} only has {sr.frame_count} frames"
                        )

                report = combined

            all_reports.append(report)
            print_report(report, verbose=args.verbose)
            if report.issues:
                zip_issues += 1
            else:
                zip_ok += 1
    else:
        logger.info("  Zips directory %s does not exist", zips_dir)

    logger.info("")
    logger.info("Zips: %d OK, %d with issues", zip_ok, zip_issues)
    logger.info("")

    # Phase 3: Games with no tiles and no zips
    logger.info("--- Phase 3: Games with no tiles at all ---")
    covered = games_with_tiles | games_in_zips
    uncovered = set(games_to_check.keys()) - covered
    for gid in sorted(uncovered):
        game = games_to_check[gid]
        expected_segs = game.get("segments", [])
        report = GameReport(
            game_id=gid,
            source="none",
            segments_expected=[segment_stem(s) for s in expected_segs],
            segments_missing=[segment_stem(s) for s in expected_segs],
            issues=[f"NO_TILES: game has no tiles on disk or in zips ({len(expected_segs)} segments)"],
        )
        all_reports.append(report)
        logger.info("  [MISSING] %s: %d segments, no tiles anywhere", gid, len(expected_segs))

    # Summary
    logger.info("")
    logger.info("=== OVERALL SUMMARY ===")
    logger.info("Registry: %d games", len(games_to_check))
    logger.info("Tiles on disk: %d games", len(games_with_tiles))
    logger.info("In zips: %d games", len(games_in_zips))
    logger.info("No tiles: %d games", len(uncovered))
    logger.info(
        "Total tiles: %s",
        f"{sum(r.total_tiles for r in all_reports):,}",
    )
    logger.info(
        "Total frames: %s",
        f"{sum(r.total_frames for r in all_reports):,}",
    )

    # Gap summary
    gaps = print_gap_summary(all_reports, registry)

    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
