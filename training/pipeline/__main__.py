"""Pipeline CLI — orchestrator and management commands.

Usage:
    uv run python -m training.pipeline status
    uv run python -m training.pipeline run [--once] [--dry-run]
    uv run python -m training.pipeline games
    uv run python -m training.pipeline queue
    uv run python -m training.pipeline machines
    uv run python -m training.pipeline retry <game_id>
    uv run python -m training.pipeline skip <game_id>
    uv run python -m training.pipeline unskip <game_id>
    uv run python -m training.pipeline enqueue <type> [--game <id>] [--machine <host>] [--priority <n>]
"""

import argparse
import logging
import sys
import time
from datetime import datetime


def cmd_status(args):
    from training.pipeline.config import load_config
    from training.pipeline.queue import WorkQueue
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    q = WorkQueue(cfg.paths.work_queue_db)
    reg = GameRegistry(cfg.paths.registry_db)

    # Workers
    workers = q.get_worker_status()
    print("=== Pipeline Status ===\n")

    print("Workers:")
    if workers:
        for w in workers:
            age = time.time() - (w.get("last_seen") or 0)
            age_str = f"{age / 60:.0f}m ago" if age < 3600 else f"{age / 3600:.1f}h ago"
            task_str = ""
            if w.get("current_task_id"):
                items = q.get_items(status="running")
                for item in items:
                    if item["id"] == w["current_task_id"]:
                        task_str = f"  {item['task_type']} {item.get('game_id', '')}"
            print(
                f"  {w['hostname']:20s} {w.get('status', '?'):10s}"
                f" GPU: {w.get('gpu_util_pct', 0):.0f}% {w.get('gpu_temp_c', 0):.0f}C"
                f" Disk: {w.get('disk_free_gb', 0):.0f}GB"
                f" ({age_str}){task_str}"
            )
    else:
        print("  (no workers reporting)")

    # Queue
    stats = q.get_queue_stats()
    print(
        f"\nQueue: {stats.get('queued', 0)} queued, {stats.get('running', 0)} running, "
        f"{stats.get('claimed', 0)} claimed, {stats.get('done', 0)} done, "
        f"{stats.get('failed', 0)} failed"
    )

    # Games
    state_counts = reg.get_state_counts()
    print(f"\nGames ({sum(state_counts.values())} total):")
    for state in [
        "TRAINABLE",
        "QA_DONE",
        "LABELED",
        "QA_PENDING",
        "LABELING",
        "TILED",
        "STAGING",
        "REGISTERED",
        "EXCLUDED",
        "HOLD",
    ]:
        count = state_counts.get(state, 0)
        if count > 0:
            print(f"  {state:15s} {count}")
    # Failed states
    for state, count in sorted(state_counts.items()):
        if state.startswith("FAILED:"):
            print(f"  {state:15s} {count}")

    # Recent events
    events = q.get_recent_events(limit=10)
    if events:
        print("\nRecent Events:")
        for e in events:
            ts = datetime.fromtimestamp(e["timestamp"]).strftime("%H:%M")
            print(f"  {ts}  {e['message']}")

    q.close()
    reg.close()


def cmd_serve(args):
    """Run the API server (must be running before orchestrator/workers)."""
    import uvicorn

    from training.pipeline.api import app, init_app
    from training.pipeline.config import load_config
    from training.pipeline.queue import WorkQueue
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    queue = WorkQueue(cfg.paths.work_queue_db)
    registry = GameRegistry(cfg.paths.registry_db)
    init_app(queue, registry, cfg)

    print(f"Starting Pipeline API on port {args.port}...")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


def cmd_run(args):
    from training.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(dry_run=args.dry_run)
    orch.run(once=args.once)


def cmd_games(args):
    from training.pipeline.config import load_config
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    reg = GameRegistry(cfg.paths.registry_db)

    games = reg.get_all_games()
    if args.state:
        games = [g for g in games if g["pipeline_state"] == args.state]

    for g in games:
        tiles = g.get("tile_count", 0)
        labels = g.get("label_count", 0)
        state = g["pipeline_state"]
        print(f"  {g['game_id']:50s} {state:15s} {tiles:>8d} tiles {labels:>8d} labels")

    print(f"\n{len(games)} games")
    reg.close()


def cmd_queue(args):
    from training.pipeline.config import load_config
    from training.pipeline.queue import WorkQueue

    cfg = load_config()
    q = WorkQueue(cfg.paths.work_queue_db)

    items = q.get_items(status=args.status, limit=args.limit)
    for item in items:
        ts = datetime.fromtimestamp(item.get("created_at") or 0).strftime("%m/%d %H:%M")
        claimed = item.get("claimed_by") or ""
        print(
            f"  #{item['id']:4d} {item['status']:10s} {item['task_type']:15s} "
            f"{item.get('game_id', ''):40s} P{item['priority']} {claimed:15s} {ts}"
        )

    stats = q.get_queue_stats()
    print(f"\nTotal: {sum(stats.values())} items ({stats})")
    q.close()


def cmd_machines(args):
    from training.pipeline.config import load_config
    from training.pipeline.queue import WorkQueue

    cfg = load_config()
    q = WorkQueue(cfg.paths.work_queue_db)

    print("Configured machines:")
    print(f"  Server: {cfg.server.hostname} ({cfg.server.ip})")
    for name, m in cfg.machines.items():
        print(f"  {name}: {m.hostname} ({m.gpu}) — {', '.join(m.capabilities)}")

    print("\nWorker status:")
    workers = q.get_worker_status()
    for w in workers:
        age = time.time() - (w.get("last_seen") or 0)
        print(f"  {w['hostname']}:")
        print(f"    Status: {w.get('status', '?')} (last seen {age / 60:.0f}m ago)")
        print(
            f"    GPU: {w.get('gpu_name', '?')} — {w.get('gpu_util_pct', 0):.0f}% util, "
            f"{w.get('gpu_temp_c', 0):.0f}C, "
            f"{w.get('gpu_memory_used_mb', 0):.0f}/{w.get('gpu_memory_total_mb', 0):.0f} MB"
        )
        print(
            f"    RAM: {w.get('ram_used_gb', 0):.1f}/{w.get('ram_total_gb', 0):.1f} GB"
        )
        print(f"    Disk: {w.get('disk_free_gb', 0):.1f} GB free")
        print(f"    User idle: {bool(w.get('is_user_idle'))}")

    q.close()


def cmd_retry(args):
    from training.pipeline.config import load_config
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    reg = GameRegistry(cfg.paths.registry_db)

    game = reg.get_game(args.game_id)
    if not game:
        print(f"Game not found: {args.game_id}")
        return

    state = game["pipeline_state"]
    if state.startswith("FAILED:"):
        new_state = state.split(":", 1)[1]
        reg.set_state(args.game_id, new_state)
        reg.reset_attempts(args.game_id)
        print(f"{args.game_id}: {state} -> {new_state}")
    else:
        print(f"{args.game_id} is not in a failed state ({state})")

    reg.close()


def cmd_skip(args):
    from training.pipeline.config import load_config
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    reg = GameRegistry(cfg.paths.registry_db)

    reg.set_state(args.game_id, "HOLD")
    print(f"{args.game_id} -> HOLD")
    reg.close()


def cmd_unskip(args):
    from training.pipeline.config import load_config
    from training.pipeline.registry import GameRegistry

    cfg = load_config()
    reg = GameRegistry(cfg.paths.registry_db)

    game = reg.get_game(args.game_id)
    if not game:
        print(f"Game not found: {args.game_id}")
        return

    # Try to infer the right state to go back to
    from training.pipeline.state_machine import infer_initial_state

    new_state = infer_initial_state(
        has_video=bool(game.get("video_path")),
        has_packs=game.get("tile_count", 0) > 0,
        has_labels=game.get("label_count", 0) > 0,
        trainable=bool(game.get("trainable")),
    )
    reg.set_state(args.game_id, new_state)
    print(f"{args.game_id}: HOLD -> {new_state}")
    reg.close()


def cmd_enqueue(args):
    from training.pipeline.config import load_config
    from training.pipeline.registry import GameRegistry
    from training.pipeline.queue import WorkQueue

    cfg = load_config()
    q = WorkQueue(cfg.paths.work_queue_db)

    # Build payload like the orchestrator does — ensures needs_flip
    # and camera_type are passed for tile/label tasks
    payload = None
    if args.type in ("tile", "label") and args.game:
        reg = GameRegistry(cfg.paths.registry_db)
        game = reg.get_game(args.game)
        reg.close()
        if game:
            payload = {
                "needs_flip": bool(game.get("needs_flip")),
                "camera_type": game.get("camera_type", "dahua"),
            }

    item_id = q.enqueue(
        args.type,
        game_id=args.game,
        priority=args.priority,
        target_machine=args.machine,
        payload=payload,
    )
    print(
        f"Enqueued {args.type} (id={item_id}, game={args.game}, priority={args.priority})"
    )
    q.close()


def cmd_priority(args):
    from training.pipeline.client import PipelineClient

    api = PipelineClient()
    api.set_priority(args.item_id, args.priority)
    print(f"#{args.item_id} priority -> {args.priority}")


def cmd_delete(args):
    from training.pipeline.client import PipelineClient

    api = PipelineClient()
    api.delete_item(args.item_id)
    print(f"Deleted #{args.item_id}")


def cmd_events(args):
    from datetime import datetime

    from training.pipeline.client import PipelineClient

    params = {}
    if args.hours:
        import time

        params["since"] = time.time() - args.hours * 3600
    if args.category:
        params["category"] = args.category
    params["limit"] = args.limit

    api = PipelineClient()
    events = api.get_events(**params)

    for e in reversed(events):
        ts = datetime.fromtimestamp(e["timestamp"]).strftime("%m/%d %H:%M")
        cat = e.get("category", "")[:12]
        msg = e.get("message", "")
        print(f"  {ts}  {cat:12s}  {msg}")


def main():
    parser = argparse.ArgumentParser(
        prog="training.pipeline", description="Pipeline orchestrator"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start API server (run first)")
    p_serve.add_argument("--port", type=int, default=8643)

    sub.add_parser("status", help="Show pipeline dashboard")

    p_run = sub.add_parser("run", help="Start orchestrator loop")
    p_run.add_argument("--once", action="store_true", help="Single pass")
    p_run.add_argument("--dry-run", action="store_true", help="Log without acting")

    p_games = sub.add_parser("games", help="List games and states")
    p_games.add_argument("--state", help="Filter by state")

    p_queue = sub.add_parser("queue", help="Show work queue")
    p_queue.add_argument("--status", help="Filter by status")
    p_queue.add_argument("--limit", type=int, default=50)

    sub.add_parser("machines", help="Show machine status")

    p_retry = sub.add_parser("retry", help="Re-queue a failed game")
    p_retry.add_argument("game_id")

    p_skip = sub.add_parser("skip", help="Put game on HOLD")
    p_skip.add_argument("game_id")

    p_unskip = sub.add_parser("unskip", help="Remove game from HOLD")
    p_unskip.add_argument("game_id")

    p_enqueue = sub.add_parser("enqueue", help="Manually enqueue a task")
    p_enqueue.add_argument("type", help="Task type (stage, tile, label, train, etc.)")
    p_enqueue.add_argument("--game", help="Game ID")
    p_enqueue.add_argument("--machine", help="Target machine hostname")
    p_enqueue.add_argument("--priority", type=int, default=50)

    p_priority = sub.add_parser("priority", help="Change queue item priority")
    p_priority.add_argument("item_id", type=int, help="Queue item ID")
    p_priority.add_argument("priority", type=int, help="New priority (lower = higher)")

    p_delete = sub.add_parser("delete", help="Delete a queue item")
    p_delete.add_argument("item_id", type=int, help="Queue item ID")

    p_events = sub.add_parser("events", help="Show pipeline event log")
    p_events.add_argument(
        "--hours",
        type=float,
        default=6,
        help="Show events from last N hours (default: 6)",
    )
    p_events.add_argument("--category", help="Filter by category (e.g. state_change)")
    p_events.add_argument("--limit", type=int, default=100, help="Max events to show")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    )

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    # File logging for long-running commands (serve, run)
    if args.command in ("serve", "run"):
        from pathlib import Path
        from logging.handlers import RotatingFileHandler
        from training.pipeline.config import load_config

        cfg = load_config()
        log_dir = Path(cfg.paths.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_name = "api.log" if args.command == "serve" else "orchestrator.log"
        # 250MB per file × 3 backups = ~1GB budget shared across 4 services
        fh = RotatingFileHandler(
            log_dir / log_name, maxBytes=250_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        handlers.append(fh)

    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=handlers)
    # Suppress httpx request logging (noisy at INFO level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    commands = {
        "serve": cmd_serve,
        "status": cmd_status,
        "run": cmd_run,
        "games": cmd_games,
        "queue": cmd_queue,
        "machines": cmd_machines,
        "retry": cmd_retry,
        "skip": cmd_skip,
        "unskip": cmd_unskip,
        "enqueue": cmd_enqueue,
        "priority": cmd_priority,
        "delete": cmd_delete,
        "events": cmd_events,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
