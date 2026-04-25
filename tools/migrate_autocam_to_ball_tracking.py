"""One-shot migration: rename ``autocam_complete`` status and ``[AUTOCAM]`` config.

Walks every ``state.json`` under the storage directory and rewrites
``status: "autocam_complete"`` to ``status: "ball_tracking_complete"``.

If a ``config.ini`` is supplied (or auto-discovered at the conventional
``shared_data/config.ini`` path), the legacy ``[AUTOCAM]`` section is
migrated to ``[BALL_TRACKING]`` + ``[BALL_TRACKING.AUTOCAM_GUI]`` so the
new pipeline picks up the user's existing executable.

Run once during deploy. Safe to re-run (idempotent).

Usage:
    uv run python -m tools.migrate_autocam_to_ball_tracking \
        --storage-path shared_data \
        --config-path shared_data/config.ini
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_autocam_to_ball_tracking")


def migrate_state_files(storage_path: Path) -> tuple[int, int]:
    """Walk every ``state.json`` and rewrite the autocam status string.

    Returns:
        (touched, scanned) — number of files updated, number scanned.
    """
    if not storage_path.is_dir():
        logger.error("Storage path is not a directory: %s", storage_path)
        return 0, 0

    touched = 0
    scanned = 0
    for state_file in storage_path.rglob("state.json"):
        scanned += 1
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping unreadable state.json at %s: %s", state_file, e)
            continue

        if data.get("status") != "autocam_complete":
            continue

        data["status"] = "ball_tracking_complete"
        try:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            touched += 1
            logger.info("Updated %s", state_file)
        except OSError as e:
            logger.error("Could not write %s: %s", state_file, e)

    return touched, scanned


def migrate_config_ini(config_path: Path) -> bool:
    """Migrate ``[AUTOCAM]`` to the new ``[BALL_TRACKING.*]`` sections.

    Returns:
        ``True`` if the file was modified, ``False`` otherwise.
    """
    if not config_path.is_file():
        logger.warning("Config file not found: %s (skipping)", config_path)
        return False

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    if not parser.has_section("AUTOCAM"):
        logger.info("No [AUTOCAM] section in %s; nothing to migrate", config_path)
        return False

    enabled = parser.get("AUTOCAM", "enabled", fallback="true")
    executable = parser.get("AUTOCAM", "executable", fallback=None)

    # Build [BALL_TRACKING] without overwriting any existing values.
    if not parser.has_section("BALL_TRACKING"):
        parser.add_section("BALL_TRACKING")
    if not parser.has_option("BALL_TRACKING", "enabled"):
        parser.set("BALL_TRACKING", "enabled", str(enabled).lower())
    if not parser.has_option("BALL_TRACKING", "provider"):
        parser.set("BALL_TRACKING", "provider", "autocam_gui")

    if executable:
        if not parser.has_section("BALL_TRACKING.AUTOCAM_GUI"):
            parser.add_section("BALL_TRACKING.AUTOCAM_GUI")
        if not parser.has_option("BALL_TRACKING.AUTOCAM_GUI", "executable"):
            parser.set("BALL_TRACKING.AUTOCAM_GUI", "executable", str(executable))

    parser.remove_section("AUTOCAM")

    backup_path = config_path.with_suffix(config_path.suffix + ".pre-ball-tracking.bak")
    try:
        backup_path.write_text(
            config_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError as e:
        logger.error("Could not write backup %s: %s", backup_path, e)
        return False

    with open(config_path, "w", encoding="utf-8") as f:
        parser.write(f)

    logger.info(
        "Migrated [AUTOCAM] -> [BALL_TRACKING] in %s (backup: %s)",
        config_path,
        backup_path.name,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--storage-path",
        type=Path,
        default=Path("shared_data"),
        help="Storage root containing per-game state.json files (default: shared_data).",
    )
    p.add_argument(
        "--config-path",
        type=Path,
        default=Path("shared_data") / "config.ini",
        help="config.ini to migrate (default: shared_data/config.ini).",
    )
    p.add_argument(
        "--skip-config",
        action="store_true",
        help="Skip config.ini migration; only update state.json files.",
    )
    args = p.parse_args(argv)

    touched, scanned = migrate_state_files(args.storage_path)
    logger.info("state.json: %d updated of %d scanned", touched, scanned)

    if not args.skip_config:
        migrate_config_ini(args.config_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
