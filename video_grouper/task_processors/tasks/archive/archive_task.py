"""Archive a published game off the working drive.

Copy -> verify -> record -> delete, strictly in that order. The ordering is
the entire point of this module, so it is spelled out here rather than left
implicit in the code below.

An earlier ad-hoc version of this deleted files first and recorded nothing.
When it was interrupted partway (a dropped remote session), it left a group
directory holding a few surviving files and a ``state.json`` that still said
``combined``. The pipeline read that as a game mid-processing and started
reprocessing it: it re-derived match info, blanked ``match_info.ini`` to a
bare stub, and would have re-rendered and re-uploaded a game that was already
published on YouTube. Nothing in the group said "this is finished, its bytes
are gone on purpose", because the only record of that lived in the same
directory being deleted.

So: the group is marked ``archived`` -- a terminal status -- *before* a single
byte is removed, and ``state.json`` is the last thing deleted. Interrupt this
at any point and what remains is a group that plainly reads as finished.
"""

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from video_grouper.models import DirectoryState, MatchInfo

from ...queue_type import QueueType
from ..base_task import BaseTask

logger = logging.getLogger(__name__)

# Read in chunks: a combined game video is tens of GB and will not fit in RAM.
_HASH_CHUNK_BYTES = 4 * 1024 * 1024


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str) -> str:
    """Make a match-info field safe to use as a directory name component."""
    cleaned = "".join(c for c in value if c not in '<>:"/\\|?*').strip()
    return cleaned or "unknown"


@dataclass(unsafe_hash=True)
class ArchiveTask(BaseTask):
    """Copy one completed group to the archive root, then reclaim its space."""

    group_dir: str

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.ARCHIVE

    @property
    def task_type(self) -> str:
        return "archive"

    def get_item_path(self) -> str:
        return self.group_dir

    def serialize(self) -> dict[str, Any]:
        return {"task_type": self.task_type, "group_dir": self.group_dir}

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> "ArchiveTask":
        return cls(group_dir=str(data["group_dir"]))

    def destination(self, archive_root: str) -> str:
        """Where this group's archive copy belongs.

        Named from ``match_info.ini`` rather than from the group directory,
        which is only a camera timestamp. Falls back to the directory name
        when match info is missing, so a game is never archived to a path
        built from empty strings.
        """
        group_name = os.path.basename(self.group_dir.rstrip("\\/"))
        match_info = MatchInfo.from_file(os.path.join(self.group_dir, "match_info.ini"))
        if match_info is None or not match_info.opponent_team_name:
            logger.warning(
                "ARCHIVE: %s has no usable match_info; archiving under its "
                "raw group name.",
                group_name,
            )
            return os.path.join(archive_root, group_name)

        date_part = group_name[:10]
        home_away = "home" if match_info.location.strip().lower() == "home" else "away"
        name = (
            f"{date_part} - vs "
            f"{_safe_component(match_info.opponent_team_name)} ({home_away})"
        )
        team = _safe_component(match_info.my_team_name)
        return os.path.join(archive_root, team, name)

    async def execute(
        self,
        archive_root: str = "",
        delete_after_verify: bool = True,
        watermark: datetime | None = None,
    ) -> bool:
        """Run the archive. Returns True when the group is fully archived.

        ``watermark`` is the camera poller's high-water mark. Archiving a
        group the watermark has not yet passed is unsafe: the poller's query
        window still covers those recordings, and every "do we have this?"
        test it can run -- ``is_file_in_state`` against the group's
        ``state.json``, or a stat of the ``.mp4`` -- reads the local disk we
        are about to empty. It would see the game as new and download it
        again. So this is a hard refusal, not a warning: the task fails, is
        retried later, and succeeds once the watermark catches up.
        """
        if not archive_root:
            logger.error("ARCHIVE: no archive path configured; refusing to run.")
            return False

        dir_state = DirectoryState(self.group_dir)
        if dir_state.status == "archived":
            logger.info(
                "ARCHIVE: %s already archived; nothing to do.",
                os.path.basename(self.group_dir),
            )
            return True

        newest = max(
            (
                f.end_time
                for f in dir_state.files.values()
                if isinstance(getattr(f, "end_time", None), datetime)
            ),
            default=None,
        )
        if newest is not None and (watermark is None or watermark < newest):
            logger.error(
                "ARCHIVE: refusing to archive %s -- the camera watermark (%s) "
                "has not passed its newest recording (%s). Archiving now would "
                "let the poller rediscover this game as new and re-download it. "
                "Will retry once the watermark advances.",
                os.path.basename(self.group_dir),
                watermark,
                newest,
            )
            return False

        dest = self.destination(archive_root)
        os.makedirs(dest, exist_ok=True)

        # ---- copy + verify ------------------------------------------------
        verified: list[str] = []
        for root, _dirs, files in os.walk(self.group_dir):
            for name in files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, self.group_dir)
                dst = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)

                if os.path.exists(dst):
                    # A previous run was interrupted. Prefer the larger file:
                    # a short destination is a truncated copy, never a source
                    # of truth, so re-copy over it.
                    if os.path.getsize(dst) < os.path.getsize(src):
                        logger.warning(
                            "ARCHIVE: %s is short at the destination "
                            "(%d < %d); re-copying.",
                            rel,
                            os.path.getsize(dst),
                            os.path.getsize(src),
                        )
                        shutil.copy2(src, dst)
                else:
                    shutil.copy2(src, dst)

                if _sha256(src) != _sha256(dst):
                    logger.error(
                        "ARCHIVE: SHA-256 mismatch for %s; aborting %s. "
                        "Nothing deleted.",
                        rel,
                        os.path.basename(self.group_dir),
                    )
                    return False
                verified.append(src)

        logger.info(
            "ARCHIVE: verified %d file(s) of %s -> %s",
            len(verified),
            os.path.basename(self.group_dir),
            dest,
        )

        # ---- record BEFORE deleting ---------------------------------------
        # See the module docstring: this is what makes an interrupted delete
        # recoverable instead of a game that reprocesses itself.
        await dir_state.update_group_status("archived")

        if not delete_after_verify:
            logger.info(
                "ARCHIVE: delete_after_verify is off; keeping the local copy of %s.",
                os.path.basename(self.group_dir),
            )
            return True

        # ---- delete, by explicit path, only what verified -----------------
        state_file = dir_state.state_file_path
        for path in verified:
            if os.path.abspath(path) == os.path.abspath(state_file):
                continue  # deleted last, below
            try:
                os.remove(path)
            except OSError as e:
                logger.error("ARCHIVE: could not remove %s: %s", path, e)
                return False

        try:
            if os.path.exists(state_file):
                os.remove(state_file)
            shutil.rmtree(self.group_dir, ignore_errors=True)
        except OSError as e:
            logger.error(
                "ARCHIVE: archived %s but could not fully remove it: %s",
                os.path.basename(self.group_dir),
                e,
            )
            return False

        logger.info(
            "ARCHIVE: %s archived to %s and reclaimed locally.",
            os.path.basename(self.group_dir),
            dest,
        )
        return True
