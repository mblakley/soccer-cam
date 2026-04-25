"""Master registry of all games, their status, and video sources.

Tracks orientation, corrected video paths, tiling status, and game phases.
The single source of truth for which games to include in training.
"""

import json
from pathlib import Path

REGISTRY_PATH = Path("F:/training_data/game_registry.json")

# Old game_id -> new game_id mapping for migration
OLD_TO_NEW = {
    "flash__06.01.2024_vs_IYSA_home": "flash__2024.06.01_vs_IYSA_home",
    "flash__09.27.2024_vs_RNYFC_Black_home": "flash__2024.09.27_vs_RNYFC_Black_home",
    "flash__09.30.2024_vs_Chili_home": "flash__2024.09.30_vs_Chili_home",
    "flash__2025.06.02": "heat__2025.06.02_vs_Fairport_home",
    "flash__2025.06.02_181603": "heat__2025.06.02_vs_Fairport_home",
    "flash__2025.05.03": "flash__2025.05.03_Saratoga_Tournament_G1",
    "flash__2025.05.04": "flash__2025.05.04_Saratoga_Tournament_G3",
    "flash__2025.05.07": "flash__2025.05.07_vs_RNY_home",
    "flash__2025.05.17": "flash__2025.05.17_vs_NY_Rush_away",
    "flash__2025.05.31": "flash__2025.05.31_vs_IYSA_away",
    "flash__2025.04.12": "flash__2025.04.12_indoor",
    "heat__05.31.2024_vs_Fairport_home": "heat__2024.05.31_vs_Fairport_home",
    "heat__06.20.2024_vs_Chili_away": "heat__2024.06.20_vs_Chili_away",
    "heat__07.17.2024_vs_Fairport_away": "heat__2024.07.17_vs_Fairport_away",
    "heat__Clarence_Tournament": "heat__2024.07.20_Clarence_Tournament",
    "heat__Heat_Tournament": "heat__2024.06.07_Heat_Tournament",
    "heat__2025.05.13": "heat__2025.05.13_vs_GUFC_home",
}


def _make_game_id(team: str, folder_name: str) -> str:
    """Generate standardized game_id from team + folder name.

    Input: 'flash', '09.27.2024 - vs RNYFC Black (home)'
    Output: 'flash__2024.09.27_vs_RNYFC_Black_home'

    Input: 'heat', '07.20.2024-07.21.2024 - Clarence Tournament'
    Output: 'heat__2024.07.20_Clarence_Tournament'
    """
    import re

    # Handle YYYY.MM.DD - description names like '2025.05.03 - vs Saratoga (Saratoga Tournament G1)'
    m = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*-\s*(.+)", folder_name)
    if m:
        date, desc = m.group(1), m.group(2)
        desc = desc.replace(" ", "_").replace("(", "").replace(")", "")
        return f"{team}__{date}_{desc}"

    # Handle date-only names like '2025.06.02-18.16.03'
    # Include time suffix to disambiguate multiple recordings on same date
    m = re.match(r"^(\d{4}\.\d{2}\.\d{2})(?:-(\d{2}\.\d{2}\.\d{2}))?", folder_name)
    if m and " - " not in folder_name:
        game_id = f"{team}__{m.group(1)}"
        if m.group(2):
            game_id += f"_{m.group(2).replace('.', '')}"
        return game_id

    # Handle tournament names: '07.20.2024-07.21.2024 - Clarence Tournament'
    m = re.match(
        r"^(\d{2})\.(\d{2})\.(\d{4})(?:-\d{2}\.\d{2}\.\d{4})?\s*-\s*(.+)", folder_name
    )
    if m:
        mm, dd, yyyy, desc = m.group(1), m.group(2), m.group(3), m.group(4)
        # Clean description
        desc = desc.replace(" ", "_").replace("(", "").replace(")", "")
        return f"{team}__{yyyy}.{mm}.{dd}_{desc}"

    # Handle standard: '09.27.2024 - vs RNYFC Black (home)'
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(.+)", folder_name)
    if m:
        mm, dd, yyyy, desc = m.group(1), m.group(2), m.group(3), m.group(4)
        desc = desc.replace(" ", "_").replace("(", "").replace(")", "")
        return f"{team}__{yyyy}.{mm}.{dd}_{desc}"

    # Fallback
    clean = (
        folder_name.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(" - ", "_")
    )
    return f"{team}__{clean}"


# Upside-down games and their corrected video sources
UPSIDE_DOWN_GAMES = {
    "05.01.2024 - vs RNYFC (away)": "flash-rnyfc-away-05-01-2024-raw.mp4",
    "05.10.2024 - vs NY Rush (away)": "combined_rotated.mp4",
    "06.01.2024 - vs IYSA (home)": "flash-iysa-home-06-01-2024-raw.mp4",
    "06.02.2024 - vs Flash 2014s (scrimmage)": "flash-flash2014s-scrimmage-06-02-2024-raw.mp4",
    "10.04.2024 - vs RNYFC MLS Next (away)": "flash-rny-mls-next-away-10-04-2024-raw.mp4",
    "10.13.2024 - vs Kenmore (away)": "flash-kenmore-away-10-13-2024-raw.mp4",
    "05.13.2024 - vs Byron Bergen (home)": "heat-bergen-home-05-13-2024-raw.mp4",
    "05.28.2024 - vs Chili (home)": "heat-chili-05-28-2024-raw-fixed2.mp4",
    "05.31.2024 - vs Fairport (home)": "flash-fairport-home-05-31-2024-raw2.mp4",
    "06.04.2024 - vs Spencerport (home)": "heat-spencerport-home-06-04-2024-raw.mp4",
    "06.27.2024 - vs Pittsford (away)": None,  # no corrected version, flip in code
    "2025.05.17 - vs NY Rush (away)": None,  # no corrected version, flip in code
    "2025.06.02 - vs Fairport (home)": None,  # no corrected version, flip in code (now in Heat_2012s)
}


def _detect_video_format(gdir: Path) -> tuple[str, list[Path]]:
    """Detect video format and find segments in a game directory.

    Returns (format, segment_list) where format is one of:
      dahua_segments, reolink_segments, dav_only, gopro, processed, none
    """
    # Dahua [F][0@0] segments (.mp4)
    dahua_mp4 = sorted(f for f in gdir.rglob("*.mp4") if "[F]" in f.name)
    if dahua_mp4:
        return "dahua_segments", dahua_mp4

    # Reolink RecM09 segments
    reolink = sorted(f for f in gdir.rglob("*.mp4") if f.name.startswith("RecM09"))
    if reolink:
        return "reolink_segments", reolink

    # Dahua .dav files (raw, need remux)
    dav_files = sorted(f for f in gdir.rglob("*.dav") if "[F]" in f.name)
    if dav_files:
        return "dav_only", dav_files

    # GoPro files (GL*.LRV or GL*.mp4)
    gopro = [f for f in gdir.rglob("*.mp4") if f.name.startswith("GL")]
    if gopro:
        return "gopro", gopro

    # Processed combined video
    processed = [f for f in gdir.glob("*raw*.mp4")] + [f for f in gdir.glob("combined*.mp4")]
    if processed:
        return "processed", processed

    return "none", []


def _classify_game(name: str, team: str, video_format: str) -> tuple[str, bool, str | None]:
    """Classify a game as trainable or excluded.

    Returns (game_type, trainable, exclude_reason).
    """
    # Indoor/dome keywords
    if "indoor" in name.lower() or "dome" in name.lower():
        return "indoor", False, "indoor dome game"

    # Futsal keywords
    if "futsal" in name.lower():
        return "futsal", False, "futsal game"

    # Non-panoramic formats
    if video_format == "gopro":
        return "outdoor_soccer", False, "GoPro recording, not panoramic"
    if video_format == "processed":
        return "outdoor_soccer", False, "only processed video, no raw segments"
    if video_format == "none":
        return "unknown", False, "no video files found"

    # Panoramic formats are trainable
    if video_format in ("dahua_segments", "reolink_segments", "dav_only"):
        return "outdoor_soccer", True, None

    return "unknown", False, f"unknown format: {video_format}"


# Camera folder timestamps that are confirmed futsal
FUTSAL_CAMERA_DATES = {
    "2025.03.03", "2025.03.10", "2025.03.17", "2025.03.24",
    "2025.03.31", "2025.04.07",  # YouTube-confirmed futsal
    "2025.01.16", "2025.02.02", "2025.02.06", "2025.02.13",
    "2025.02.22", "2025.02.27",  # Frame-check confirmed indoor
}

# Camera folder timestamps that are confirmed non-game
NON_GAME_CAMERA = {
    "2024.11.01", "2025.01.23", "2025.01.25", "2025.02.01",
    "2025.02.26", "2025.03.02", "2025.03.25", "2025.04.01",
}


def build_registry() -> list[dict]:
    """Scan F: drive and build complete game registry."""
    bases = [
        ("flash", Path("F:/Flash_2013s")),
        ("heat", Path("F:/Heat_2012s")),
        ("guest", Path("F:/Guest")),
    ]
    games = []

    for team, base in bases:
        if not base.exists():
            continue

        for gdir in sorted(base.iterdir()):
            if not gdir.is_dir():
                continue

            video_format, segments = _detect_video_format(gdir)
            if video_format == "none":
                continue

            name = gdir.name
            is_upside_down = name in UPSIDE_DOWN_GAMES

            # Determine video source for tiling
            if is_upside_down:
                corrected = UPSIDE_DOWN_GAMES[name]
                if corrected:
                    corrected_path = gdir / corrected
                    if corrected_path.exists():
                        video_source = "corrected"
                        video_path = str(corrected_path)
                    else:
                        video_source = "flip_in_code"
                        video_path = None
                else:
                    video_source = "flip_in_code"
                    video_path = None
            else:
                video_source = "segments"
                video_path = None

            game_id = _make_game_id(team, name)
            game_type, trainable, exclude_reason = _classify_game(
                name, team, video_format
            )

            # Check existing tiles and labels
            tiles_dir_d = Path("D:/training_data/tiles_640") / game_id
            tiles_dir_f = Path("F:/training_data/tiles_640") / game_id
            has_tiles = tiles_dir_d.exists() or tiles_dir_f.exists()

            labels_dir_d = Path("D:/training_data/labels_640_ext") / game_id
            labels_dir_f = Path("F:/training_data/labels_640_ext") / game_id
            has_labels = labels_dir_d.exists() or labels_dir_f.exists()

            games.append({
                "game_id": game_id,
                "name": name,
                "team": team,
                "path": str(gdir),
                "segments": [s.name for s in segments],
                "segment_count": len(segments),
                "orientation": "upside_down" if is_upside_down else "right_side_up",
                "video_source": video_source,
                "video_format": video_format,
                "corrected_video": video_path,
                "needs_flip": video_source == "flip_in_code",
                "game_type": game_type,
                "trainable": trainable,
                "has_tiles": has_tiles,
                "has_labels": has_labels,
                "exclude": not trainable,
                "exclude_reason": exclude_reason,
            })

    # Scan Camera directory for futsal/indoor games (excluded from training)
    camera_dir = Path("F:/Camera")
    if camera_dir.exists():
        for gdir in sorted(camera_dir.iterdir()):
            if not gdir.is_dir():
                continue

            video_format, segments = _detect_video_format(gdir)
            if video_format == "none":
                continue

            name = gdir.name
            date_prefix = name[:10]  # YYYY.MM.DD

            if date_prefix in FUTSAL_CAMERA_DATES:
                game_type = "futsal"
                exclude_reason = "futsal/indoor game"
            elif date_prefix in NON_GAME_CAMERA:
                game_type = "false_trigger"
                exclude_reason = "not a game"
            else:
                game_type = "unknown_camera"
                exclude_reason = "unclassified camera recording"

            game_id = _make_game_id("camera", name)

            games.append({
                "game_id": game_id,
                "name": name,
                "team": "camera",
                "path": str(gdir),
                "segments": [s.name for s in segments],
                "segment_count": len(segments),
                "orientation": "right_side_up",
                "video_source": "segments",
                "video_format": video_format,
                "corrected_video": None,
                "needs_flip": False,
                "game_type": game_type,
                "trainable": False,
                "has_tiles": False,
                "has_labels": False,
                "exclude": True,
                "exclude_reason": exclude_reason,
            })

    return games


def save_registry(games: list[dict]):
    """Save registry to disk."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(games, f, indent=2)


def load_registry() -> list[dict]:
    """Load registry from disk."""
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def main():
    games = build_registry()
    save_registry(games)

    trainable = [g for g in games if g["trainable"]]
    excluded = [g for g in games if g["exclude"]]
    tiled = [g for g in trainable if g["has_tiles"]]
    labeled = [g for g in trainable if g["has_labels"]]

    by_team = {}
    for g in games:
        by_team.setdefault(g["team"], []).append(g)

    by_format = {}
    for g in games:
        by_format.setdefault(g["video_format"], []).append(g)

    by_type = {}
    for g in games:
        by_type.setdefault(g["game_type"], []).append(g)

    print(f"Total entries: {len(games)}")
    print(f"  Trainable: {len(trainable)}")
    print(f"  Excluded: {len(excluded)}")
    print()
    print("By team:")
    for team, gs in sorted(by_team.items()):
        t = sum(1 for g in gs if g["trainable"])
        print(f"  {team}: {len(gs)} total, {t} trainable")
    print()
    print("By format:")
    for fmt, gs in sorted(by_format.items()):
        print(f"  {fmt}: {len(gs)}")
    print()
    print("By type:")
    for gt, gs in sorted(by_type.items()):
        print(f"  {gt}: {len(gs)}")
    print()
    print(f"Trainable games: {len(trainable)} ({len(tiled)} tiled, {len(labeled)} labeled)")
    print(f"Need tiling: {len(trainable) - len(tiled)}")
    print(f"Need labeling: {len(trainable) - len(labeled)}")
    print(f"\nRegistry saved to {REGISTRY_PATH}")


if __name__ == "__main__":
    main()
