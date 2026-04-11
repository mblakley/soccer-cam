"""Shared I/O helpers for tasks — standardize pull-local-process-push pattern.

Every task uses these helpers to:
1. Resolve where game data lives (local D: or remote share)
2. Pull files to local SSD before processing
3. Push results back to server after processing
4. Clean up local working files

This ensures consistent behavior across all tasks and all machines.
"""

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class TaskIO:
    """Manages local working directory and server data access for a task.

    Usage:
        io = TaskIO(game_id="flash__2024.06.01_vs_IYSA_home",
                     local_work_dir=Path("G:/pipeline_work"),
                     server_share="\\\\192.168.86.152\\training")
        io.ensure_space(needed_gb=5)

        # Pull what you need
        io.pull_manifest()
        io.pull_packs()
        io.pull_video()

        # Process locally — all paths via io.local_*
        do_work(io.local_manifest, io.local_packs)

        # Push results
        io.push_manifest()
        io.push_packs()

        # Cleanup
        io.cleanup()
    """

    def __init__(
        self,
        game_id: str,
        local_work_dir: Path,
        server_share: str = "",
    ):
        self.game_id = game_id
        self.local_work_dir = Path(local_work_dir)
        self.server_share = server_share

        # Load config for paths
        from training.pipeline.config import load_config
        self.cfg = load_config()

        # Server game directory (D: on server, share for remote)
        self._server_games_dir = Path(self.cfg.paths.games_dir)
        if server_share and not self._server_games_dir.exists():
            self._server_games_dir = Path(server_share) / "games"

        self.server_game_dir = self._server_games_dir / game_id

        # Local working paths (all on SSD)
        self.local_game = self.local_work_dir / game_id
        self.local_manifest_path = self.local_game / "manifest.db"
        self.local_packs = self.local_game / "tile_packs"
        self.local_video = self.local_game / "video"

    # ------------------------------------------------------------------
    # Resolve paths
    # ------------------------------------------------------------------

    def server_manifest(self) -> Path:
        return self.server_game_dir / "manifest.db"

    def server_packs(self) -> Path:
        """Find pack files — check D: per-game dir, restore from F: archive if needed.

        On the server: checks D: first, then restores from F: archive.
        On remote workers: checks D: via SMB share. F: is not accessible
        remotely — the server must have restored packs to D: first.
        """
        packs = self.server_game_dir / "tile_packs"
        if packs.exists() and any(packs.glob("*.pack")):
            return packs

        # Only try F: archive if we're on the server (no server_share = local)
        if not self.server_share:
            archive_dir = Path(self.cfg.paths.archive.tile_packs) / self.game_id
            if archive_dir.exists() and any(archive_dir.glob("*.pack")):
                packs.mkdir(parents=True, exist_ok=True)
                for pack_file in archive_dir.glob("*.pack"):
                    dest = packs / pack_file.name
                    if not dest.exists():
                        logger.info("Restoring %s from F: archive (%.1f GB)",
                                    pack_file.name, pack_file.stat().st_size / 1e9)
                        shutil.copy2(str(pack_file), str(dest))
                return packs

        return packs  # default even if empty

    def video_path(self) -> Path | None:
        """Find original video files — uses API to look up video path."""
        from training.pipeline.client import PipelineClient

        api_url = self.cfg.server.ip
        # Use worker's API URL if available, otherwise construct from config
        client = PipelineClient(f"http://{api_url}:8643")
        game = client.get_game(self.game_id)

        if not game or not game.get("video_path"):
            return None

        vpath = Path(game["video_path"])

        # If running on server, F: is directly accessible
        if vpath.exists():
            return vpath

        # Remote: map F: paths through video share
        if self.server_share:
            vpath_str = str(vpath)
            video_share = self.cfg.server.share_video
            for prefix in ["F:\\", "F:/"]:
                if vpath_str.startswith(prefix):
                    mapped = Path(vpath_str.replace(prefix, video_share + "\\", 1))
                    if mapped.exists():
                        return mapped

        return None

    # ------------------------------------------------------------------
    # Space check
    # ------------------------------------------------------------------

    def ensure_space(self, needed_gb: float = 5.0):
        """Check that local work dir has enough free space."""
        self.local_game.mkdir(parents=True, exist_ok=True)
        _, _, free = shutil.disk_usage(str(self.local_work_dir))
        free_gb = free / (1024**3)
        if free_gb < needed_gb:
            raise OSError(
                f"Insufficient disk space on {self.local_work_dir}: "
                f"{free_gb:.1f}GB free, need {needed_gb:.1f}GB"
            )

    # ------------------------------------------------------------------
    # Pull (server → local SSD)
    # ------------------------------------------------------------------

    def pull_manifest(self) -> Path:
        """Copy manifest.db from server to local SSD."""
        src = self.server_manifest()
        if not src.exists():
            raise FileNotFoundError(f"Server manifest not found: {src}")
        self.local_game.mkdir(parents=True, exist_ok=True)
        # Remove stale WAL/SHM from previous runs before copying fresh manifest
        for suffix in ("-wal", "-shm"):
            stale = Path(str(self.local_manifest_path) + suffix)
            if stale.exists():
                stale.unlink()
        shutil.copy2(str(src), str(self.local_manifest_path))
        logger.debug("Pulled manifest.db for %s (%.1f MB)",
                     self.game_id, os.path.getsize(str(self.local_manifest_path)) / 1e6)
        return self.local_manifest_path

    def pull_packs(self) -> Path:
        """Copy pack files from server to local SSD."""
        src = self.server_packs()
        self.local_packs.mkdir(parents=True, exist_ok=True)
        count = 0
        for pack_file in src.glob("*.pack"):
            dest = self.local_packs / pack_file.name
            if not dest.exists():
                shutil.copy2(str(pack_file), str(dest))
                count += 1
        logger.debug("Pulled %d pack files for %s", count, self.game_id)
        return self.local_packs

    def pull_video(self) -> Path:
        """Copy video files from F: (or share) to local SSD."""
        src = self.video_path()
        if src is None:
            raise FileNotFoundError(f"No video path found for {self.game_id}")

        self.local_video.mkdir(parents=True, exist_ok=True)
        video_files = sorted(src.glob("*.mp4")) + sorted(src.glob("*.dav"))
        if not video_files:
            video_files = sorted(src.rglob("*.mp4"))

        count = 0
        for vf in video_files:
            dest = self.local_video / vf.name
            if not dest.exists():
                shutil.copy2(str(vf), str(dest))
                count += 1
        logger.debug("Pulled %d video files for %s", count, self.game_id)
        return self.local_video

    # ------------------------------------------------------------------
    # Push (local SSD → server)
    # ------------------------------------------------------------------

    def push_manifest(self):
        """Copy manifest.db from local SSD back to server."""
        dest = self.server_manifest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(self.local_manifest_path), str(dest))
        logger.debug("Pushed manifest.db for %s", self.game_id)

    def push_packs(self):
        """Copy pack files from local SSD to server per-game dir."""
        dest = self.server_game_dir / "tile_packs"
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        for pack_file in self.local_packs.glob("*.pack"):
            shutil.copy2(str(pack_file), str(dest / pack_file.name))
            count += 1
        logger.debug("Pushed %d pack files for %s", count, self.game_id)
        return dest

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Remove local working directory for this game."""
        if self.local_game.exists():
            try:
                shutil.rmtree(str(self.local_game))
                logger.debug("Cleaned up %s", self.local_game)
            except Exception as e:
                logger.warning("Cleanup failed for %s: %s", self.local_game, e)

    def cleanup_server_packs(self):
        """Remove packs from D: after use (they're archived on F:).

        Only deletes if the F: archive copy exists and matches size.
        """
        server_packs = self.server_game_dir / "tile_packs"
        if not server_packs.exists():
            return

        archive_dir = Path(self.cfg.paths.archive.tile_packs) / self.game_id
        if not archive_dir.exists():
            return  # no archive — keep D: packs

        for pack_file in server_packs.glob("*.pack"):
            archived = archive_dir / pack_file.name
            if archived.exists() and archived.stat().st_size == pack_file.stat().st_size:
                pack_file.unlink()
                logger.debug("Cleaned D: pack %s (archived on F:)", pack_file.name)
            else:
                logger.debug("Keeping D: pack %s (no matching archive)", pack_file.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def local_pack_path_for(self, pack_filename: str) -> Path:
        """Get local SSD path for a pack file (for rewriting manifest references)."""
        return self.local_packs / pack_filename

    def server_pack_path_for(self, pack_filename: str) -> Path:
        """Get server destination path for a pack file."""
        return self.server_game_dir / "tile_packs" / pack_filename
