"""Shared recording storage manager for camera simulators.

Manages video recordings on disk with JSON metadata index.
Supports upload, search, delete, test pattern generation,
and auto-seeding from mounted clip directories.
"""

import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "/data/recordings"
METADATA_FILE = "/data/recordings.json"


class StorageManager:
    """Manages video recordings for camera simulators."""

    def __init__(
        self,
        recordings_dir: str = RECORDINGS_DIR,
        metadata_file: str = METADATA_FILE,
        camera_type: str = "reolink",
    ):
        self.recordings_dir = recordings_dir
        self.metadata_file = metadata_file
        self.camera_type = camera_type
        self._recordings: list[dict] = []
        os.makedirs(recordings_dir, exist_ok=True)
        os.makedirs(os.path.dirname(metadata_file), exist_ok=True)
        self._load_metadata()

    def _load_metadata(self):
        if os.path.exists(self.metadata_file):
            try:
                with open(self.metadata_file) as f:
                    self._recordings = json.load(f)
                logger.info(f"Loaded {len(self._recordings)} recordings from metadata")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load metadata: {e}")
                self._recordings = []

    def _save_metadata(self):
        try:
            with open(self.metadata_file, "w") as f:
                json.dump(self._recordings, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save metadata: {e}")

    def _generate_camera_path(self, start_time: datetime, end_time: datetime) -> str:
        """Generate a camera-style file path based on camera type."""
        if self.camera_type == "reolink":
            date_str = start_time.strftime("%Y-%m-%d")
            start_str = start_time.strftime("%H%M%S")
            end_str = end_time.strftime("%H%M%S")
            return f"/mnt/sda/Mp4Record/{date_str}/RecS01_{start_str}_{end_str}.mp4"
        else:
            start_str = start_time.strftime("%H.%M.%S")
            end_str = end_time.strftime("%H.%M.%S")
            seq = len(self._recordings) + 134510
            return f"/mnt/dvr/{start_str}-{end_str}[F][0@0][{seq}].dav"

    @property
    def recordings(self) -> list[dict]:
        return self._recordings.copy()

    def add_recording(
        self,
        file_path: str,
        start_time: datetime,
        end_time: datetime,
        channel: int = 0,
        camera_path: Optional[str] = None,
    ) -> dict:
        """Add a recording from an uploaded or generated file."""
        rec_id = str(uuid.uuid4())[:8]
        filename = os.path.basename(file_path)

        # Copy to recordings dir if not already there
        dest = os.path.join(self.recordings_dir, f"{rec_id}_{filename}")
        if os.path.abspath(file_path) != os.path.abspath(dest):
            with open(file_path, "rb") as src, open(dest, "wb") as dst:
                while chunk := src.read(65536):
                    dst.write(chunk)

        size = os.path.getsize(dest)
        if camera_path is None:
            camera_path = self._generate_camera_path(start_time, end_time)

        record = {
            "id": rec_id,
            "filename": filename,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "size": size,
            "channel": channel,
            "file_path": dest,
            "camera_path": camera_path,
        }
        self._recordings.append(record)
        self._save_metadata()
        logger.info(f"Added recording {rec_id}: {filename} ({size} bytes)")
        return record

    def search(
        self,
        start_time: datetime,
        end_time: datetime,
        channel: Optional[int] = None,
    ) -> list[dict]:
        """Search recordings within a time range."""
        results = []
        for rec in self._recordings:
            rec_start = datetime.fromisoformat(rec["start_time"])
            rec_end = datetime.fromisoformat(rec["end_time"])

            # Strip timezone for comparison if needed
            if rec_start.tzinfo and not start_time.tzinfo:
                rec_start = rec_start.replace(tzinfo=None)
                rec_end = rec_end.replace(tzinfo=None)
            elif not rec_start.tzinfo and start_time.tzinfo:
                start_time = start_time.replace(tzinfo=None)
                end_time = end_time.replace(tzinfo=None)

            # Check overlap
            if rec_start < end_time and rec_end > start_time:
                if channel is None or rec.get("channel") == channel:
                    results.append(rec)
        return results

    def get_file(self, id_or_path: str) -> Optional[str]:
        """Get the local file path for a recording by ID or camera path."""
        for rec in self._recordings:
            if rec["id"] == id_or_path or rec["camera_path"] == id_or_path:
                if os.path.exists(rec["file_path"]):
                    return rec["file_path"]
        # Try matching by filename in camera_path
        basename = os.path.basename(id_or_path)
        for rec in self._recordings:
            if os.path.basename(rec["camera_path"]) == basename:
                if os.path.exists(rec["file_path"]):
                    return rec["file_path"]
        return None

    def delete_recording(self, rec_id: str) -> bool:
        """Remove a recording and its file."""
        for i, rec in enumerate(self._recordings):
            if rec["id"] == rec_id:
                try:
                    if os.path.exists(rec["file_path"]):
                        os.remove(rec["file_path"])
                except OSError as e:
                    logger.warning(f"Failed to delete file: {e}")
                self._recordings.pop(i)
                self._save_metadata()
                logger.info(f"Deleted recording {rec_id}")
                return True
        return False

    def generate_test_recording(
        self,
        start_time: datetime,
        duration: int = 60,
        channel: int = 0,
    ) -> Optional[dict]:
        """Generate a test pattern video file via ffmpeg."""
        end_time = start_time + timedelta(seconds=duration)
        camera_path = self._generate_camera_path(start_time, end_time)
        rec_id = str(uuid.uuid4())[:8]
        filename = f"test_{rec_id}.mp4"
        dest = os.path.join(self.recordings_dir, filename)

        try:
            cmd = [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=duration={duration}:size=1920x1080:rate=25",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-y",
                dest,
            ]
            subprocess.run(cmd, capture_output=True, check=True, timeout=120)
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as e:
            logger.error(f"Failed to generate test recording: {e}")
            return None

        size = os.path.getsize(dest)
        record = {
            "id": rec_id,
            "filename": filename,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "size": size,
            "channel": channel,
            "file_path": dest,
            "camera_path": camera_path,
        }
        self._recordings.append(record)
        self._save_metadata()
        logger.info(f"Generated test recording {rec_id}: {duration}s ({size} bytes)")
        return record

    def seed_from_clips(self, clips_dir: str) -> int:
        """Auto-seed recordings from a mounted clips directory.

        By default, creates 6 files in 2 groups of 3 (10-second gap between
        groups), timestamped 12 hours ago. Matches the SimulatorCamera seeding
        logic so E2E test expectations hold.

        Override defaults with environment variables:
          SEED_BASE_TIME  -- ISO datetime for first file (e.g. 2025-07-22T18:08:14+00:00)
          SEED_DURATION   -- per-file duration in seconds (default 60)
          SEED_GAP        -- gap between groups in seconds (default 10)
          SEED_GROUP_SIZE -- files per group (default 3)
          SEED_FILE_COUNT -- total number of files (default 6)

        When SEED_FILE_COUNT is set to match the number of available clips,
        each clip is used exactly once (no cycling). Otherwise clips are
        cycled as needed.
        """
        if not os.path.isdir(clips_dir):
            logger.warning(f"Clips directory not found: {clips_dir}")
            return 0

        clip_files = sorted(
            [
                os.path.join(clips_dir, f)
                for f in os.listdir(clips_dir)
                if f.endswith(".mp4") and "backup" not in f
            ]
        )
        if not clip_files:
            logger.warning(f"No .mp4 clips found in {clips_dir}")
            return 0

        # Skip if already seeded
        if self._recordings:
            logger.info("Recordings already exist, skipping seed")
            return 0

        # Read overrides from environment
        env_base_time = os.environ.get("SEED_BASE_TIME")
        file_duration = int(os.environ.get("SEED_DURATION", "60"))
        gap_seconds = int(os.environ.get("SEED_GAP", "10"))
        group_size = int(os.environ.get("SEED_GROUP_SIZE", "3"))
        file_count = int(os.environ.get("SEED_FILE_COUNT", "6"))

        if env_base_time:
            base_time = datetime.fromisoformat(env_base_time)
            logger.info(f"Using SEED_BASE_TIME={env_base_time}")
        else:
            utc_now = datetime.now(timezone.utc).replace(microsecond=0)
            base_time = utc_now - timedelta(hours=12)

        current_time = base_time
        count = 0

        for i in range(file_count):
            # Insert gap between groups (at each group boundary after the first)
            if group_size > 0 and i > 0 and i % group_size == 0 and gap_seconds > 0:
                current_time = current_time + timedelta(seconds=gap_seconds)

            start_time = current_time
            end_time = current_time + timedelta(seconds=file_duration)
            camera_path = self._generate_camera_path(start_time, end_time)

            clip_path = clip_files[i % len(clip_files)]
            rec_id = str(uuid.uuid4())[:8]
            clip_filename = os.path.basename(clip_path)
            dest = os.path.join(self.recordings_dir, f"{rec_id}_{clip_filename}")

            try:
                with open(clip_path, "rb") as src, open(dest, "wb") as dst:
                    while chunk := src.read(65536):
                        dst.write(chunk)
            except OSError as e:
                logger.error(f"Failed to copy clip {clip_path}: {e}")
                continue

            size = os.path.getsize(dest)
            record = {
                "id": rec_id,
                "filename": clip_filename,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "size": size,
                "channel": 0,
                "file_path": dest,
                "camera_path": camera_path,
            }
            self._recordings.append(record)
            count += 1
            current_time = end_time

        self._save_metadata()
        logger.info(
            f"Seeded {count} recordings from {clips_dir} "
            f"({len(clip_files)} clips, duration={file_duration}s, "
            f"gap={gap_seconds}s, group_size={group_size})"
        )
        return count
