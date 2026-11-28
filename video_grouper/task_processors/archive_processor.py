"""Processor for archiving published games off the working drive.

Sits after upload: a group reaches ``complete`` when its videos are on
YouTube, and only then is it safe to reclaim the local footage. Runs on its
own queue because verifying a game means hashing tens of gigabytes, which
must not sit on the upload path.
"""

import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime

from video_grouper.utils.config import Config
from video_grouper.utils.locking import FileLock
from video_grouper.utils.paths import get_camera_state_path

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.archive import ArchiveTask
from .tasks.base_task import BaseTask

logger = logging.getLogger(__name__)

_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ArchiveProcessor(QueueProcessor):
    """Copies completed groups to the archive root and reclaims their space."""

    def __init__(self, storage_path: str, config: Config):
        super().__init__(storage_path, config)

    @property
    def queue_type(self) -> QueueType:
        return QueueType.ARCHIVE

    def get_item_key(self, item: BaseTask) -> str:
        return f"archive:{item.get_item_path()}"

    def _read_watermark(
        self, camera_names: Iterable[str] | None = None
    ) -> datetime | None:
        """The watermark that is safe to archive behind: the slowest camera's.

        The archive guard needs "has the poller finished with this time
        range?", and with several cameras the answer is only yes when the
        *slowest* one has passed it, so this is the minimum.

        Only cameras in ``self.config.cameras`` count. ``camera_state.json``
        keeps an entry for every camera that ever ran, and a removed or
        renamed camera's last mark never moves again; taking it into the
        minimum would block archiving forever.

        ``camera_names`` narrows it further to the cameras that recorded the
        group (see :meth:`ArchiveTask.recording_cameras`); None means every
        configured camera. A camera in that set with no readable mark has not
        finished with anything, so the answer is None -- refuse -- rather
        than the minimum of the others.
        """
        configured = [c.name for c in getattr(self.config, "cameras", None) or []]
        if camera_names is None:
            relevant = set(configured)
        else:
            relevant = set(camera_names)
            unknown = relevant - set(configured)
            if unknown:
                # A removed camera has no poller left to rediscover anything,
                # so it needs no watermark of its own; the configured ones do.
                logger.warning(
                    "ARCHIVE: group recorded by camera(s) %s that are no longer "
                    "configured; using every configured camera's watermark.",
                    sorted(unknown),
                )
                relevant = set(configured)
        if not relevant:
            logger.error("ARCHIVE: no cameras configured; no watermark to trust.")
            return None

        state_path = get_camera_state_path(self.storage_path)
        if not os.path.exists(state_path):
            return None
        try:
            with FileLock(state_path):
                with open(state_path) as fh:
                    all_state = json.load(fh)
        except Exception as e:  # noqa: BLE001
            logger.error("ARCHIVE: cannot read camera state: %s", e)
            return None
        if not isinstance(all_state, dict):
            return None

        marks: list[datetime] = []
        for name in sorted(relevant):
            value = all_state.get(name)
            raw = value.get("latest_video_time") if isinstance(value, dict) else None
            try:
                marks.append(datetime.strptime(str(raw).strip(), _DATE_FORMAT))
            except (TypeError, ValueError):
                logger.info(
                    "ARCHIVE: camera %r has no watermark yet; nothing can be "
                    "reclaimed behind it.",
                    name,
                )
                return None
        return min(marks)

    async def process_item(self, item: BaseTask) -> None:
        if not isinstance(item, ArchiveTask):
            logger.error("ARCHIVE: unexpected task %r; ignoring.", item)
            return

        archive_cfg = self.config.archive
        if archive_cfg.after_upload == "keep":
            logger.debug(
                "ARCHIVE: [ARCHIVE] after_upload = keep; leaving %s alone.",
                item.group_dir,
            )
            return

        # Hard-fail on a relative or vanished group_dir before anything reads
        # from it: a relative one resolves against the working directory.
        item.require_real_group_dir()

        # Archives are per-team; resolve this game's root from its
        # my_team_name before anything is copied or deleted.
        team = item.team_name()
        team_root = archive_cfg.root_for_team(team)
        if archive_cfg.makes_second_copy and not team_root:
            # Refuse rather than dumping the game into some other team's
            # archive: the roots are age-group specific and mixing them is
            # not something a later pass can untangle.
            raise RuntimeError(
                f"[ARCHIVE] no archive root for team {team!r} "
                f"({item.group_dir}). Add it under [ARCHIVE.PER_TEAM], or set "
                "a fallback `path`."
            )

        ok = await item.execute(
            archive_root=team_root,
            make_second_copy=archive_cfg.makes_second_copy,
            reclaim_local_space=archive_cfg.reclaims_local_space,
            watermark=self._read_watermark(item.recording_cameras()),
        )
        if not ok:
            # Raise so the base class applies its retry/backoff. A refusal on
            # the watermark guard is expected and temporary — the retry will
            # succeed once the poller settles the recordings ahead of it.
            raise RuntimeError(f"Archive incomplete for {item.group_dir}")
