"""Pipeline orchestrator — keeps all machines busy automatically.

Long-running process on the server that:
1. Discovers what work needs doing (unlabeled games, stale training sets)
2. Checks which machines are available
3. Stages data and submits jobs
4. Collects results and merges them
5. Builds training sets and deploys to laptop
6. Sends NTFY notifications on milestones

Usage:
    uv run python -m training.pipeline.orchestrator
    uv run python -m training.pipeline.orchestrator --once  # single pass
    uv run python -m training.pipeline.orchestrator --status  # show state
"""

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from training.data_prep.manifest import (
    backup_db,
    merge_labels_from,
    open_db,
)
from training.data_prep.manifest_dataset import build_training_set
from training.pipeline.machine_manager import MachineManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("D:/training_data/orchestrator.log"),
    ],
)
logger = logging.getLogger(__name__)

# Paths
MASTER_DB = Path("D:/training_data/manifest.db")
MASTER_PACKS = Path("D:/training_data/tile_packs")
TRAINING_SETS_DIR = Path("D:/training_data/training_sets")
ARCHIVE_DIR = Path("F:/training_sets")
GAME_REGISTRY = Path("D:/training_data/game_registry.json")
STATE_PATH = Path("D:/training_data/orchestrator_state.json")

# Config
CHECK_INTERVAL = 300  # seconds between orchestrator loops
FORTNITE_OP_LABEL_DB = "D:\\labeling\\manifest.db"
FORTNITE_OP_LABEL_LOG = "D:\\labeling\\label_log.txt"
FORTNITE_OP_VIDEO_DIR = "D:\\labeling"
LAPTOP_TRAINING_DIR = "C:\\soccer-cam-label"

mgr = MachineManager()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "last_merge_time": None,
        "last_training_set_version": None,
        "last_training_set_label_count": 0,
        "last_training_deploy_time": None,
        "training_in_progress": False,
        "current_fortnite_game": None,
        "games_labeled_by_fortnite": [],
        "total_labels_merged": 0,
    }


def save_state(state: dict):
    state["last_updated"] = datetime.now().isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))


def get_games_needing_labels(conn) -> list[str]:
    """Find packed games with tiles but no/few labels."""
    rows = conn.execute("""
        SELECT g.game_id, g.tile_count,
               (SELECT COUNT(*) FROM labels WHERE game_id = g.game_id) as label_count
        FROM games g
        WHERE g.tiles_cataloged IS NOT NULL AND g.tile_count > 0
        ORDER BY g.game_id
    """).fetchall()
    # Games with <1% labeled tiles need labeling
    return [gid for gid, tc, lc in rows if tc > 0 and lc / tc < 0.01]


def get_games_with_labels(conn) -> list[dict]:
    """Find packed games that have labels (for training sets)."""
    rows = conn.execute("""
        SELECT g.game_id, g.tile_count,
               (SELECT COUNT(DISTINCT tile_stem) FROM labels WHERE game_id = g.game_id) as label_count
        FROM games g
        WHERE g.tiles_cataloged IS NOT NULL AND g.tile_count > 0
        ORDER BY g.game_id
    """).fetchall()
    return [
        {"game_id": gid, "tiles": tc, "labels": lc} for gid, tc, lc in rows if lc > 100
    ]


def find_video_for_game(game_id: str) -> list[str]:
    """Find video segment paths for a game on F: drive."""
    registry = json.loads(GAME_REGISTRY.read_text())
    game = next((g for g in registry if g["game_id"] == game_id), None)
    if not game:
        return []

    # Search for segments in staging or F: drive
    staging = Path(f"D:/training_data/staging/{game_id}")
    if staging.exists():
        # Resolve symlinks
        paths = []
        for f in staging.glob("*.mp4"):
            target = f.resolve() if f.is_symlink() else f
            if target.exists() and target.stat().st_size > 0:
                paths.append(str(target))
        if paths:
            return paths

    # Search F: drive for segments
    for segment in game.get("segments", []):
        # Segments are on F: in various subdirectories
        for search_dir in Path("F:/").iterdir():
            if not search_dir.is_dir():
                continue
            for subdir in search_dir.iterdir():
                if not subdir.is_dir():
                    continue
                seg_file = subdir / segment
                if seg_file.exists():
                    return [str(f) for f in subdir.glob("*[F][0@0]*.mp4")]

    return []


# ── Orchestrator actions ──────────────────────────────────────────


def action_check_fortnite_labeling(state: dict) -> dict:
    """Check FORTNITE-OP: collect results, submit next job if idle."""
    if not mgr.is_online("FORTNITE-OP"):
        logger.debug("FORTNITE-OP offline")
        return state

    task_state = mgr.check_task("FORTNITE-OP", "RunLabeling")

    if task_state == "Running":
        # Still labeling — check progress
        log = mgr.get_log_tail("FORTNITE-OP", FORTNITE_OP_LABEL_LOG, 5)
        if log:
            logger.info("FORTNITE-OP labeling: %s", log.split("\n")[-1][:100])
        return state

    # Task is Ready (done or never started)
    # Check if there are labels to collect
    code, output = mgr.remote_exec(
        "FORTNITE-OP",
        f"""
        if (Test-Path "{FORTNITE_OP_LABEL_DB}") {{
            $sz = (Get-Item "{FORTNITE_OP_LABEL_DB}").Length
            Write-Output "DB_SIZE:$sz"
        }} else {{
            Write-Output "NO_DB"
        }}
    """,
    )

    if "DB_SIZE:" in output:
        db_size = int(output.split("DB_SIZE:")[1].strip())
        if db_size > 4096:  # More than empty DB
            # Pull and merge labels
            logger.info(
                "FORTNITE-OP has labels to collect (DB: %d KB)", db_size // 1024
            )
            local_remote_db = Path(
                "D:/training_data/remote_manifests/fortnite_op_manifest.db"
            )
            local_remote_db.parent.mkdir(parents=True, exist_ok=True)

            if mgr.pull_file("FORTNITE-OP", FORTNITE_OP_LABEL_DB, str(local_remote_db)):
                conn = open_db(MASTER_DB)
                backup_db(MASTER_DB)
                result = merge_labels_from(conn, local_remote_db)
                conn.close()
                state["total_labels_merged"] += result["labels_inserted"]
                state["last_merge_time"] = datetime.now().isoformat()
                logger.info(
                    "Merged %d labels from FORTNITE-OP", result["labels_inserted"]
                )
                mgr.send_ntfy(
                    f"Merged {result['labels_inserted']} labels from FORTNITE-OP. "
                    f"Total: {state['total_labels_merged']}",
                    title="Labels Merged",
                )

    # Check if we should submit the next labeling job
    if not mgr.is_idle("FORTNITE-OP"):
        logger.info("FORTNITE-OP not idle (game running)")
        return state

    conn = open_db(MASTER_DB)
    games_needing = get_games_needing_labels(conn)
    conn.close()

    # Filter out games already done or in progress
    done = set(state.get("games_labeled_by_fortnite", []))
    todo = [g for g in games_needing if g not in done]

    if not todo:
        logger.info("FORTNITE-OP: no games need labeling")
        return state

    next_game = todo[0]
    logger.info("FORTNITE-OP: staging %s for labeling", next_game)

    # Find and stage video
    video_paths = find_video_for_game(next_game)
    if not video_paths:
        logger.warning("No video found for %s, skipping", next_game)
        return state

    if mgr.stage_video("FORTNITE-OP", next_game, video_paths):
        # Update the batch file on FORTNITE-OP to process this game
        mgr.remote_exec(
            "FORTNITE-OP",
            f"""
            Remove-Item D:\\labeling\\manifest.db -ErrorAction SilentlyContinue
            Remove-Item D:\\labeling\\label_log.txt -ErrorAction SilentlyContinue
            @"
C:\\Python313\\python.exe -u C:\\soccer-cam-label\\label_job.py --video-dir "D:\\labeling\\{next_game}" --game-id "{next_game}" --model "C:\\soccer-cam-label\\models\\model.onnx" --db "D:\\labeling\\manifest.db" > D:\\labeling\\label_log.txt 2>&1
"@ | Set-Content C:\\tmp\\run_label.bat -Encoding ASCII
            Start-ScheduledTask -TaskName "RunLabeling"
            Write-Output "STARTED"
        """,
            timeout=30,
        )

        state["current_fortnite_game"] = next_game
        logger.info("FORTNITE-OP: started labeling %s", next_game)

    return state


def action_check_laptop_training(state: dict) -> dict:
    """Check laptop: is training done? should we deploy new training set?"""
    if not mgr.is_online("jared-laptop"):
        logger.debug("Laptop offline")
        return state

    task_state = mgr.check_task("jared-laptop", "TrainV3")

    if task_state == "Running":
        # Check progress
        log = mgr.get_log_tail(
            "jared-laptop", f"{LAPTOP_TRAINING_DIR}\\train_v3.log", 3
        )
        if log:
            logger.info("Laptop training: %s", log.split("\n")[-1][:100])
        return state

    # Training not running — check if we have new labels to train on
    conn = open_db(MASTER_DB)
    current_label_count = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    conn.close()

    if current_label_count <= state.get("last_training_set_label_count", 0):
        logger.debug("No new labels since last training set")
        return state

    # New labels available — should we build a new training set?
    labeled_games = _get_labeled_packed_games()
    if len(labeled_games) < 3:
        logger.info(
            "Only %d labeled games, need at least 3 for training", len(labeled_games)
        )
        return state

    # Build new training set
    version = f"v3.{int(time.time()) % 10000}"
    output_dir = TRAINING_SETS_DIR / version
    logger.info(
        "Building training set %s (%d labeled games, %d labels)",
        version,
        len(labeled_games),
        current_label_count,
    )

    val_game = labeled_games[-1]  # Last game as val
    train_games = labeled_games[:-1]

    # Get camera games for negative diversity
    camera_games = [
        d.name
        for d in MASTER_PACKS.iterdir()
        if d.is_dir() and d.name.startswith("camera__")
    ]

    build_training_set(
        master_db=str(MASTER_DB),
        master_packs=str(MASTER_PACKS),
        output_dir=str(output_dir),
        train_games=train_games,
        val_games=[val_game],
        neg_ratio=1.0,
        camera_neg_games=camera_games[:2] if camera_games else None,
    )

    # Archive to F:
    archive_path = ARCHIVE_DIR / version
    if ARCHIVE_DIR.exists():
        import shutil

        shutil.copytree(str(output_dir), str(archive_path), dirs_exist_ok=True)
        logger.info("Archived training set to %s", archive_path)

    # Deploy to laptop
    logger.info("Deploying training set to laptop...")
    if mgr.push_directory(
        "jared-laptop", str(output_dir), f"{LAPTOP_TRAINING_DIR}\\training_set"
    ):
        # Start training
        mgr.remote_exec(
            "jared-laptop",
            """
            Start-ScheduledTask -TaskName "TrainV3"
            Write-Output "STARTED"
        """,
        )
        state["last_training_set_version"] = version
        state["last_training_set_label_count"] = current_label_count
        state["last_training_deploy_time"] = datetime.now().isoformat()
        state["training_in_progress"] = True
        logger.info("Training deployed and started: %s", version)
        mgr.send_ntfy(
            f"Training set {version} deployed to laptop. "
            f"{len(train_games)} train + 1 val games, {current_label_count} labels.",
            title="Training Started",
        )

    return state


def _get_labeled_packed_games() -> list[str]:
    """Get list of packed games that have labels."""
    conn = open_db(MASTER_DB)
    games = []
    for d in sorted(MASTER_PACKS.iterdir()):
        if not d.is_dir() or not any(d.glob("*.pack")):
            continue
        gid = d.name
        lc = conn.execute(
            "SELECT COUNT(*) FROM labels WHERE game_id=?", (gid,)
        ).fetchone()[0]
        if lc > 100:
            games.append(gid)
    conn.close()
    return games


def action_collect_training_results(state: dict) -> dict:
    """Check if laptop training completed and collect weights."""
    if not state.get("training_in_progress"):
        return state
    if not mgr.is_online("jared-laptop"):
        return state

    task_state = mgr.check_task("jared-laptop", "TrainV3")
    if task_state == "Running":
        return state

    # Training completed — collect weights
    logger.info("Training completed on laptop, collecting weights...")
    version = state.get("last_training_set_version", "unknown")
    weights_dir = Path(f"D:/training_data/models/{version}")
    weights_dir.mkdir(parents=True, exist_ok=True)

    # Try to find best.pt on laptop
    code, output = mgr.remote_exec(
        "jared-laptop",
        """
        Get-ChildItem -Recurse C:\\soccer-cam-label\\training_set\\runs -Filter "best.pt" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    """,
    )

    if output and "best.pt" in output:
        remote_weights = output.strip()
        local_weights = weights_dir / "best.pt"
        if mgr.pull_file("jared-laptop", remote_weights, str(local_weights)):
            logger.info("Collected weights: %s", local_weights)
            state["training_in_progress"] = False
            mgr.send_ntfy(
                f"Training {version} complete! Weights saved to {local_weights}",
                title="Training Complete",
            )
    else:
        logger.warning("Could not find best.pt on laptop")
        state["training_in_progress"] = False

    return state


def print_status():
    """Print current pipeline status."""
    state = load_state()
    print(f"\n=== Pipeline Status ({state.get('last_updated', 'never')}) ===")
    print(f"  Total labels merged:    {state.get('total_labels_merged', 0):,}")
    print(f"  Last merge:             {state.get('last_merge_time', 'never')}")
    print(f"  Training set:           {state.get('last_training_set_version', 'none')}")
    print(f"  Training in progress:   {state.get('training_in_progress', False)}")
    print(f"  FORTNITE-OP game:       {state.get('current_fortnite_game', 'none')}")
    print(
        f"  Games labeled:          {len(state.get('games_labeled_by_fortnite', []))}"
    )

    # Check machine status
    print("\n  Machines:")
    for hostname in ["FORTNITE-OP", "jared-laptop"]:
        online = mgr.is_online(hostname)
        print(f"    {hostname}: {'ONLINE' if online else 'OFFLINE'}")


def run_once(state: dict) -> dict:
    """Run one orchestration pass."""
    logger.info("=== Orchestrator pass ===")

    try:
        state = action_check_fortnite_labeling(state)
    except Exception as e:
        logger.error("FORTNITE-OP check failed: %s", e)

    try:
        state = action_collect_training_results(state)
    except Exception as e:
        logger.error("Training result collection failed: %s", e)

    try:
        state = action_check_laptop_training(state)
    except Exception as e:
        logger.error("Laptop training check failed: %s", e)

    save_state(state)
    return state


def main():
    parser = argparse.ArgumentParser(description="Training pipeline orchestrator")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL,
        help=f"Seconds between checks (default: {CHECK_INTERVAL})",
    )
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    state = load_state()

    if args.once:
        state = run_once(state)
        return

    logger.info("Pipeline orchestrator starting (interval: %ds)", args.interval)
    mgr.send_ntfy("Pipeline orchestrator started", title="Pipeline")

    while True:
        try:
            state = run_once(state)
        except Exception as e:
            logger.error("Orchestrator error: %s", e)
            mgr.send_ntfy(f"Orchestrator error: {e}", title="Pipeline Error")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
