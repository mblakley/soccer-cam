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
    "flash__2025.06.02": "flash__2025.06.02_181603",
    "heat__05.31.2024_vs_Fairport_home": "heat__2024.05.31_vs_Fairport_home",
    "heat__06.20.2024_vs_Chili_away": "heat__2024.06.20_vs_Chili_away",
    "heat__07.17.2024_vs_Fairport_away": "heat__2024.07.17_vs_Fairport_away",
    "heat__Clarence_Tournament": "heat__2024.07.20_Clarence_Tournament",
    "heat__Heat_Tournament": "heat__2024.06.07_Heat_Tournament",
}


def _make_game_id(team: str, folder_name: str) -> str:
    """Generate standardized game_id from team + folder name.

    Input: 'flash', '09.27.2024 - vs RNYFC Black (home)'
    Output: 'flash__2024.09.27_vs_RNYFC_Black_home'

    Input: 'heat', '07.20.2024-07.21.2024 - Clarence Tournament'
    Output: 'heat__2024.07.20_Clarence_Tournament'
    """
    import re

    # Handle date-only names like '2025.06.02-18.16.03'
    # Include time suffix to disambiguate multiple recordings on same date
    m = re.match(r"^(\d{4}\.\d{2}\.\d{2})(?:-(\d{2}\.\d{2}\.\d{2}))?", folder_name)
    if m and " - " not in folder_name:
        game_id = f"{team}__{m.group(1)}"
        if m.group(2):
            game_id += f"_{m.group(2).replace('.', '')}"
        return game_id

    # Handle tournament names: '07.20.2024-07.21.2024 - Clarence Tournament'
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})(?:-\d{2}\.\d{2}\.\d{4})?\s*-\s*(.+)", folder_name)
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
    clean = folder_name.replace(" ", "_").replace("(", "").replace(")", "").replace(" - ", "_")
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
    "2025.05.17-16.49.52": None,  # no corrected version, flip in code
    "2025.06.02-18.16.03": None,  # no corrected version, flip in code
}


def build_registry() -> list[dict]:
    """Scan F: drive and build complete game registry."""
    bases = [Path("F:/Flash_2013s"), Path("F:/Heat_2012s")]
    games = []

    for base in bases:
        if not base.exists():
            continue
        team = "flash" if "Flash" in base.name else "heat"

        for gdir in sorted(base.iterdir()):
            if not gdir.is_dir():
                continue

            # Find [F] video segments (recursive for tournament sub-game folders)
            segments = sorted(
                [f for f in gdir.rglob("*.mp4") if "[F]" in f.name]
            )
            if not segments:
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
                        video_path = None  # use segments with flip
                else:
                    video_source = "flip_in_code"
                    video_path = None
            else:
                video_source = "segments"
                video_path = None  # use [F] segments directly

            # Generate game_id per naming convention:
            # {team}__{YYYY.MM.DD}_vs_{opponent}_{location}
            game_id = _make_game_id(team, name)

            # Check existing tiles (D: is primary, F: is fallback)
            # Use dir existence as proxy — glob on USB is very slow
            tiles_dir_d = Path("D:/training_data/tiles_640") / game_id
            tiles_dir_f = Path("F:/training_data/tiles_640") / game_id
            has_tiles = tiles_dir_d.exists() or tiles_dir_f.exists()

            # Check existing labels
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
                "corrected_video": video_path,
                "needs_flip": video_source == "flip_in_code",
                "has_tiles": has_tiles,
                "has_labels": has_labels,
                "exclude": False,
                "exclude_reason": None,
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

    # Summary
    right_up = [g for g in games if g["orientation"] == "right_side_up"]
    flipped_corrected = [g for g in games if g["video_source"] == "corrected"]
    flipped_code = [g for g in games if g["needs_flip"]]
    tiled = [g for g in games if g["has_tiles"]]
    labeled = [g for g in games if g["has_labels"]]

    print(f"Total games: {len(games)}")
    print(f"  Right-side up: {len(right_up)}")
    print(f"  Upside-down (corrected video): {len(flipped_corrected)}")
    print(f"  Upside-down (flip in code): {len(flipped_code)}")
    print(f"  Already tiled: {len(tiled)}")
    print(f"  Already labeled: {len(labeled)}")
    print(f"  Need tiling: {len(games) - len(tiled)}")
    print(f"  Need labeling: {len(games) - len(labeled)}")
    print(f"\nRegistry saved to {REGISTRY_PATH}")


if __name__ == "__main__":
    main()
