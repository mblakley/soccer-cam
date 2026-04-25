"""Show pipeline completion status for every game through all sequential stages.

Usage:
    uv run python -m training.pipeline.progress
    uv run python -m training.pipeline.progress --team flash
    uv run python -m training.pipeline.progress --game flash__2024.06.30_vs_IYSA_away
"""

import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

# Pipeline stages in order
STAGES = [
    "staged",  # Video source cataloged
    "tiled",  # Frames extracted to tile packs
    "labeled",  # ONNX ball detection run
    "qa_done",  # Sonnet QA verified labels
    "phases",  # Game phases detected
    "field_mask",  # Field boundary polygon set
    "trainable",  # Ready for training
]

STAGE_LABELS = {
    "staged": "Staged",
    "tiled": "Tiled",
    "labeled": "Labeled",
    "qa_done": "QA",
    "phases": "Phases",
    "field_mask": "Field",
    "trainable": "Ready",
}

# States that imply a stage is complete
STATE_IMPLIES = {
    "STAGING": {"staged"},
    "TILED": {"staged", "tiled"},
    "LABELING": {"staged", "tiled"},
    "LABELED": {"staged", "tiled", "labeled"},
    "QA_PENDING": {"staged", "tiled", "labeled"},
    "QA_DONE": {"staged", "tiled", "labeled", "qa_done"},
    "REVIEW_PENDING": {"staged", "tiled", "labeled", "qa_done"},
    "TRAINABLE": {"staged", "tiled", "labeled", "qa_done"},
}


def get_game_progress(game: dict, games_dir: Path) -> dict:
    """Determine which stages are complete for a game."""
    gid = game["game_id"]
    state = game["pipeline_state"]
    completed = set(STATE_IMPLIES.get(state, set()))

    # Check manifest for actual data presence
    manifest_path = games_dir / gid / "manifest.db"
    tiles = 0
    labels = 0
    has_phases = False
    has_field = False
    segments = 0

    if manifest_path.exists():
        from training.data_prep.game_manifest import GameManifest

        gm = GameManifest(games_dir / gid)
        try:
            gm.open(create=False)
            tiles = gm.get_tile_count()
            labels = gm.get_label_count()
            segments = len(gm.get_segments())
            has_phases = len(gm.get_phases()) > 0
            has_field = gm.get_metadata("field_boundary") is not None
            gm.close()
        except Exception:
            pass

    # Override based on actual data (don't trust state alone)
    if tiles > 0 and segments > 0:
        completed.add("tiled")
    if labels > 0:
        completed.add("labeled")
    if has_phases:
        completed.add("phases")
    if has_field:
        completed.add("field_mask")
    if state == "TRAINABLE" and tiles > 0 and labels > 0:
        completed.add("trainable")

    return {
        "game_id": gid,
        "state": state,
        "completed": completed,
        "tiles": tiles,
        "labels": labels,
        "segments": segments,
        "has_phases": has_phases,
        "has_field": has_field,
    }


def render_progress(info: dict) -> str:
    """Render a single game's progress as a one-line bar."""
    gid = info["game_id"]
    completed = info["completed"]

    bar = ""
    for stage in STAGES:
        if stage in completed:
            bar += f" \033[32m[{STAGE_LABELS[stage]}]\033[0m"
        else:
            bar += f" \033[90m[{STAGE_LABELS[stage]}]\033[0m"

    details = (
        f"  tiles={info['tiles']:,}  labels={info['labels']:,}  segs={info['segments']}"
    )

    return f"{gid:52s} {bar}  {details}"


def main():
    parser = argparse.ArgumentParser(description="Pipeline game progress")
    parser.add_argument("--game", help="Show a single game")
    parser.add_argument("--team", help="Filter by team (flash/heat)")
    parser.add_argument(
        "--incomplete", action="store_true", help="Only show games missing stages"
    )
    args = parser.parse_args()

    from training.pipeline.client import PipelineClient
    from training.pipeline.config import load_config

    cfg = load_config()
    client = PipelineClient()
    games_dir = Path(cfg.paths.games_dir)

    all_games = client.get_all_games()

    # Filter
    if args.game:
        all_games = [g for g in all_games if g["game_id"] == args.game]
    if args.team:
        all_games = [g for g in all_games if g["game_id"].startswith(args.team)]

    # Exclude EXCLUDED games
    all_games = [g for g in all_games if g["pipeline_state"] != "EXCLUDED"]

    # Sort by team then date
    all_games.sort(key=lambda g: g["game_id"])

    # Header
    header_bar = ""
    for stage in STAGES:
        header_bar += f" [{STAGE_LABELS[stage]:>6s}]"
    print(f"{'Game':52s} {header_bar}  {'Details'}")
    print("-" * 140)

    complete_count = 0
    total = len(all_games)

    for game in all_games:
        info = get_game_progress(game, games_dir)
        if args.incomplete and len(info["completed"]) == len(STAGES):
            complete_count += 1
            continue
        line = render_progress(info)
        print(line)
        if len(info["completed"]) == len(STAGES):
            complete_count += 1

    print("-" * 140)
    print(
        f"Complete: {complete_count}/{total}  |  Stages: {' > '.join(STAGE_LABELS[s] for s in STAGES)}"
    )


if __name__ == "__main__":
    main()
