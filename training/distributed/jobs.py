"""Filesystem-based job queue for distributed GPU/CPU workers.

Jobs are JSON files in a shared directory. Workers claim jobs by
atomically renaming them from pending/ to active/. No coordinator needed.

Job types: label, tile, train, infer, qa

Submit jobs:
    uv run python -m training.distributed.jobs submit label --games all
    uv run python -m training.distributed.jobs submit tile --games heat__Heat_Tournament
    uv run python -m training.distributed.jobs submit train --config configs/ball_v5.yaml
    uv run python -m training.distributed.jobs list
    uv run python -m training.distributed.jobs status
"""

import argparse
import json
import logging
import os
import socket
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- Paths ----
SHARE = os.environ.get("SHARE_PATH", "//192.168.86.152/video")
JOBS_DIR = Path(os.environ.get("JOBS_DIR", f"{SHARE}/training_data/worker_jobs"))
PENDING = JOBS_DIR / "pending"
ACTIVE = JOBS_DIR / "active"
DONE = JOBS_DIR / "done"
FAILED = JOBS_DIR / "failed"

MODEL_PATH = os.environ.get("BALL_MODEL_PATH", "")
LABELS_DIR = f"{SHARE}/training_data/labels_640_ext"
TILES_DIR = f"{SHARE}/training_data/tiles_640"

GAMES = {
    "flash__06.01.2024_vs_IYSA_home": f"{SHARE}/Flash_2013s/06.01.2024 - vs IYSA (home)",
    "flash__09.27.2024_vs_RNYFC_Black_home": f"{SHARE}/Flash_2013s/09.27.2024 - vs RNYFC Black (home)",
    "flash__09.30.2024_vs_Chili_home": f"{SHARE}/Flash_2013s/09.30.2024 - vs Chili (home)",
    "flash__2025.06.02": f"{SHARE}/Flash_2013s/2025.06.02-18.16.03",
    "heat__05.31.2024_vs_Fairport_home": f"{SHARE}/Heat_2012s/05.31.2024 - vs Fairport (home)",
    "heat__06.20.2024_vs_Chili_away": f"{SHARE}/Heat_2012s/06.20.2024 - vs Chili (away)",
    "heat__07.17.2024_vs_Fairport_away": f"{SHARE}/Heat_2012s/07.17.2024 - vs Fairport (away)",
    "heat__Clarence_Tournament": f"{SHARE}/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament",
    "heat__Heat_Tournament": f"{SHARE}/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament",
}


# ---- Job creation ----


def make_job(
    job_type: str,
    priority: int = 5,
    requires: list[str] | None = None,
    **kwargs,
) -> dict:
    """Create a job dict."""
    return {
        "type": job_type,
        "priority": priority,
        "requires": requires or [],
        "created": time.time(),
        "created_by": socket.gethostname(),
        **kwargs,
    }


def submit_job(job: dict) -> Path:
    """Write a job file to pending/. Returns the job file path."""
    PENDING.mkdir(parents=True, exist_ok=True)
    # Filename: priority_type_timestamp_random.json
    ts = int(time.time() * 1000)
    rand = os.urandom(4).hex()
    name = f"{job['priority']:02d}_{job['type']}_{ts}_{rand}.json"
    path = PENDING / name
    with open(path, "w") as f:
        json.dump(job, f, indent=2)
    return path


def submit_label_jobs(game_ids: list[str]) -> int:
    """Submit label jobs for unlabeled segments in the given games."""
    import glob as glob_mod

    count = 0
    for game_id in game_ids:
        video_src = GAMES.get(game_id)
        if not video_src:
            logger.warning("Unknown game: %s", game_id)
            continue
        video_dir = Path(video_src)
        if not video_dir.exists():
            logger.warning("Video dir not found: %s", video_src)
            continue
        label_dir = Path(LABELS_DIR) / game_id
        segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
        for seg in segments:
            # Check if already labeled
            if label_dir.exists():
                escaped = glob_mod.escape(seg.stem)
                if list(label_dir.glob(f"{escaped}_frame_*.txt")):
                    continue
            # Check if job already pending/active
            if _job_exists("label", game_id=game_id, segment=seg.stem):
                continue
            job = make_job(
                "label",
                priority=3,
                requires=["gpu"],
                game_id=game_id,
                segment=seg.stem,
                video_path=str(seg),
                model_path=MODEL_PATH,
                output_dir=str(Path(LABELS_DIR) / game_id),
            )
            submit_job(job)
            count += 1
            logger.info("  label: %s/%s", game_id, seg.stem[:40])
    return count


def submit_tile_jobs(game_ids: list[str]) -> int:
    """Submit tile jobs for untiled segments in the given games."""
    import glob as glob_mod

    count = 0
    for game_id in game_ids:
        video_src = GAMES.get(game_id)
        if not video_src:
            continue
        video_dir = Path(video_src)
        if not video_dir.exists():
            continue
        tile_dir = Path(TILES_DIR) / game_id
        segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
        for seg in segments:
            if tile_dir.exists():
                escaped = glob_mod.escape(seg.stem)
                if list(tile_dir.glob(f"{escaped}_frame_000000_r0_c0.jpg")):
                    continue
            if _job_exists("tile", game_id=game_id, segment=seg.stem):
                continue
            job = make_job(
                "tile",
                priority=7,  # lower priority than labeling
                game_id=game_id,
                segment=seg.stem,
                video_path=str(seg),
                output_dir=str(Path(TILES_DIR) / game_id),
            )
            submit_job(job)
            count += 1
            logger.info("  tile: %s/%s", game_id, seg.stem[:40])
    return count


def submit_train_job(config_path: str, **kwargs) -> Path:
    """Submit a training job."""
    job = make_job(
        "train",
        priority=1,  # highest priority
        requires=["gpu"],
        config_path=config_path,
        **kwargs,
    )
    return submit_job(job)


def _job_exists(job_type: str, **match_fields) -> bool:
    """Check if a matching job is already pending or active."""
    for d in [PENDING, ACTIVE]:
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.load(open(f))
                if data.get("type") != job_type:
                    continue
                if all(data.get(k) == v for k, v in match_fields.items()):
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    return False


# ---- Job claiming (for workers) ----


def claim_job(capabilities: list[str] | None = None) -> tuple[dict, Path] | None:
    """Claim the highest-priority pending job this worker can handle.

    Returns (job_dict, active_path) or None if no work available.
    Claiming is atomic via os.rename (same filesystem).
    """
    caps = set(capabilities or [])
    if not PENDING.exists():
        return None

    # Sort by filename (priority is first field)
    candidates = sorted(PENDING.glob("*.json"))
    for job_path in candidates:
        try:
            data = json.load(open(job_path))
        except (json.JSONDecodeError, OSError):
            continue

        # Check capabilities
        required = set(data.get("requires", []))
        if required and not required.issubset(caps):
            continue

        # Try to claim by renaming to active/
        ACTIVE.mkdir(parents=True, exist_ok=True)
        active_path = ACTIVE / job_path.name
        try:
            os.rename(str(job_path), str(active_path))
        except OSError:
            continue  # another worker got it

        # Stamp with worker info
        data["claimed_by"] = socket.gethostname()
        data["claimed_at"] = time.time()
        with open(active_path, "w") as f:
            json.dump(data, f, indent=2)

        return data, active_path

    return None


def complete_job(active_path: Path, result: dict | None = None):
    """Move job from active/ to done/."""
    DONE.mkdir(parents=True, exist_ok=True)
    if result:
        data = json.load(open(active_path))
        data["result"] = result
        data["completed_at"] = time.time()
        with open(active_path, "w") as f:
            json.dump(data, f, indent=2)
    try:
        os.rename(str(active_path), str(DONE / active_path.name))
    except OSError:
        pass


def fail_job(active_path: Path, error: str):
    """Move job from active/ to failed/."""
    FAILED.mkdir(parents=True, exist_ok=True)
    try:
        data = json.load(open(active_path))
        data["error"] = error
        data["failed_at"] = time.time()
        with open(active_path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
    try:
        os.rename(str(active_path), str(FAILED / active_path.name))
    except OSError:
        pass


# ---- Status ----


def get_status() -> dict:
    """Get job queue status."""
    status = {}
    for name, d in [
        ("pending", PENDING),
        ("active", ACTIVE),
        ("done", DONE),
        ("failed", FAILED),
    ]:
        if not d.exists():
            status[name] = []
            continue
        jobs = []
        for f in sorted(d.glob("*.json")):
            try:
                jobs.append(json.load(open(f)))
            except (json.JSONDecodeError, OSError):
                pass
        status[name] = jobs
    return status


def print_status():
    """Print human-readable job queue status."""
    s = get_status()
    print(
        f"\nPending: {len(s['pending'])}  Active: {len(s['active'])}  "
        f"Done: {len(s['done'])}  Failed: {len(s['failed'])}"
    )

    if s["active"]:
        print("\nActive jobs:")
        for j in s["active"]:
            worker = j.get("claimed_by", "?")
            elapsed = time.time() - j.get("claimed_at", time.time())
            print(
                f"  [{j['type']}] {j.get('game_id', '')}/{j.get('segment', j.get('config_path', ''))[:40]} "
                f"on {worker} ({elapsed / 60:.0f}m)"
            )

    if s["pending"]:
        # Summarize by type
        by_type = {}
        for j in s["pending"]:
            t = j["type"]
            by_type[t] = by_type.get(t, 0) + 1
        print("\nPending by type:")
        for t, n in sorted(by_type.items()):
            print(f"  {t}: {n}")

    if s["failed"]:
        print(f"\nFailed jobs: {len(s['failed'])}")
        for j in s["failed"][-5:]:
            print(
                f"  [{j['type']}] {j.get('game_id', '')} - {j.get('error', '?')[:60]}"
            )


# ---- CLI ----


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(description="Job queue manager")
    sub = parser.add_subparsers(dest="command")

    # submit
    submit_p = sub.add_parser("submit", help="Submit jobs")
    submit_p.add_argument("job_type", choices=["label", "tile", "train"])
    submit_p.add_argument(
        "--games", nargs="*", default=["all"], help="Game IDs or 'all'"
    )
    submit_p.add_argument("--config", help="Training config path (for train jobs)")

    # status
    sub.add_parser("status", help="Show queue status")

    # list
    sub.add_parser("list", help="List all pending jobs")

    # clean
    clean_p = sub.add_parser("clean", help="Clean done/failed jobs")
    clean_p.add_argument("--failed", action="store_true", help="Also retry failed jobs")

    args = parser.parse_args()

    if args.command == "submit":
        game_ids = list(GAMES.keys()) if "all" in args.games else args.games

        if args.job_type == "label":
            n = submit_label_jobs(game_ids)
            print(f"Submitted {n} label jobs")
        elif args.job_type == "tile":
            n = submit_tile_jobs(game_ids)
            print(f"Submitted {n} tile jobs")
        elif args.job_type == "train":
            if not args.config:
                print("--config required for train jobs")
                return
            p = submit_train_job(args.config)
            print(f"Submitted train job: {p.name}")

    elif args.command == "status":
        print_status()

    elif args.command == "list":
        if PENDING.exists():
            for f in sorted(PENDING.glob("*.json")):
                j = json.load(open(f))
                reqs = ",".join(j.get("requires", [])) or "cpu"
                print(
                    f"  [{j['type']}] {j.get('game_id', '')}/{j.get('segment', '')[:30]} ({reqs})"
                )
        else:
            print("No pending jobs")

    elif args.command == "clean":
        for d in [DONE] + ([FAILED] if not args.failed else []):
            if d.exists():
                for f in d.glob("*.json"):
                    f.unlink()
        if args.failed and FAILED.exists():
            # Move failed back to pending for retry
            for f in FAILED.glob("*.json"):
                os.rename(str(f), str(PENDING / f.name))
            print("Failed jobs moved back to pending for retry")
        print("Cleaned")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
