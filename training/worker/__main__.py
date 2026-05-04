"""Worker CLI — run on each machine to pull and execute pipeline tasks.

Usage:
    uv run python -m training.worker run
    uv run python -m training.worker run --once
    uv run python -m training.worker run --config path/to/worker_config.toml
    uv run python -m training.worker status
    uv run python -m training.worker capabilities
"""

import argparse
import logging
import platform
from pathlib import Path


def cmd_run(args):
    from training.worker.worker import Worker

    if args.config:
        worker = Worker.from_config(Path(args.config))
    else:
        # Try default config locations
        for candidate in [
            Path("worker_config.toml"),
            Path("C:/soccer-cam-label/worker_config.toml"),
            Path(__file__).parent / "server_worker_config.toml",  # server default
        ]:
            if candidate.exists():
                worker = Worker.from_config(candidate)
                break
        else:
            # Fall back to server defaults using main pipeline config
            from training.pipeline.config import load_config

            cfg = load_config()
            hostname = platform.node()

            # Find this machine in config
            caps = []
            for name, machine in cfg.machines.items():
                if machine.hostname == hostname:
                    caps = machine.capabilities
                    break

            if not caps:
                # Server defaults
                caps = ["stage", "tile", "sonnet_qa", "merge"]

            worker = Worker(
                hostname=hostname,
                capabilities=caps,
                queue_db=cfg.paths.work_queue_db,
                local_work_dir=cfg.paths.server_work_dir,
                server_share=cfg.server.share_training,
                max_gpu_temp=85,
                min_disk_free_gb=20,
            )

    worker.run(once=args.once)


def cmd_status(args):
    from training.pipeline.config import load_config
    from training.pipeline.queue import WorkQueue

    cfg = load_config()
    q = WorkQueue(cfg.paths.work_queue_db)
    hostname = platform.node()

    workers = q.get_worker_status(hostname)
    if workers:
        w = workers[0]
        print(f"Worker: {w['hostname']}")
        print(f"Status: {w['status']}")
        print(
            f"GPU: {w['gpu_name']} ({w['gpu_util_pct']:.0f}%, {w['gpu_temp_c']:.0f}C)"
        )
        print(f"RAM: {w['ram_used_gb']:.1f}/{w['ram_total_gb']:.1f} GB")
        print(f"Disk free: {w['disk_free_gb']:.1f} GB")
        print(f"User idle: {bool(w['is_user_idle'])}")
        if w["current_task_id"]:
            items = q.get_items(status="running")
            for item in items:
                if item["id"] == w["current_task_id"]:
                    print(
                        f"Current task: {item['task_type']} {item.get('game_id', '')}"
                    )
    else:
        print(f"No status found for {hostname}")
    q.close()


def cmd_capabilities(args):
    from training.pipeline.config import load_config

    cfg = load_config()
    hostname = platform.node()

    print(f"Machine: {hostname}")
    for name, machine in cfg.machines.items():
        if machine.hostname == hostname:
            print(f"Config name: {name}")
            print(f"GPU: {machine.gpu}")
            print(f"Capabilities: {', '.join(machine.capabilities)}")
            return

    print("Not found in pipeline config — using server defaults")
    print("Capabilities: stage, tile, sonnet_qa, merge")


def main():
    parser = argparse.ArgumentParser(
        prog="training.worker", description="Pipeline worker"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Start worker loop")
    p_run.add_argument("--once", action="store_true", help="Process one item and exit")
    p_run.add_argument("--config", help="Path to worker_config.toml")

    sub.add_parser("status", help="Show worker status")
    sub.add_parser("capabilities", help="Show machine capabilities")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    )

    # Stream handler with flush for redirected output
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.flush = lambda: sh.stream.flush()
    handlers: list[logging.Handler] = [sh]

    # File logging for the run command (log_dir from worker config TOML)
    if args.command == "run":
        import tomllib
        from logging.handlers import RotatingFileHandler

        config_path = getattr(args, "config", "") or ""
        log_dir_str = None
        if config_path:
            with open(config_path, "rb") as _f:
                _raw = tomllib.load(_f)
            log_dir_str = _raw.get("logging", {}).get("log_dir")

        if log_dir_str:
            _log_dir = Path(log_dir_str)
            _log_dir.mkdir(parents=True, exist_ok=True)
            if "qa" in config_path:
                log_name = "qa_worker.log"
            else:
                log_name = "worker.log"
            # 250MB per file × 3 backups = ~1GB budget shared across 4 services
            fh = RotatingFileHandler(
                _log_dir / log_name, maxBytes=250_000_000, backupCount=3
            )
            fh.setFormatter(fmt)
            handlers.append(fh)

    logging.basicConfig(level=level, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "capabilities":
        cmd_capabilities(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
