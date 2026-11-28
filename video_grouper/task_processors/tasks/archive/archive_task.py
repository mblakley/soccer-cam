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
at any point and what remains is a group that plainly reads as finished. A
retry of an ``archived`` group whose local files are still there goes back
through copy + verify and finishes the reclaim.

Each archive folder belongs to exactly one source group, recorded in a marker
file written before the first byte is copied. Folder names come from match
info and are not unique -- two games on one day against one opponent share
one -- so a folder another group (or nobody) claims is never written into;
the next free " (2)", " (3)" ... is used instead.
"""

import hashlib
import json
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

# Written into each archive folder; names the source group that owns it.
ARCHIVE_MARKER = ".soccer-cam-archive.json"
# Upper bound on " (n)" suffixes tried for one game name. Far beyond any real
# number of same-day games against one opponent; hitting it means something
# is wrong, and the task refuses rather than guessing.
_MAX_DESTINATION_SUFFIX = 50
# Deleted after every other local file (and before state.json), so an
# interrupted reclaim still knows its game's name -- and so its archive
# folder -- on the retry.
_MATCH_INFO = "match_info.ini"


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

    def require_real_group_dir(self) -> None:
        """Hard-fail unless ``group_dir`` is an absolute, existing directory.

        A relative path resolves against the process's working directory, not
        the storage path: it names nothing, the walk finds no files, and an
        empty group would be recorded ``archived`` there and reported done.
        """
        if not os.path.isabs(self.group_dir):
            raise ValueError(
                f"ARCHIVE: group_dir must be an absolute path, got "
                f"{self.group_dir!r}; resolve it against the storage path first."
            )
        if not os.path.isdir(self.group_dir):
            raise ValueError(f"ARCHIVE: group_dir {self.group_dir!r} does not exist.")

    def team_name(self) -> str:
        """This group's ``my_team_name``, used to pick its archive root."""
        match_info = MatchInfo.from_file(os.path.join(self.group_dir, _MATCH_INFO))
        return match_info.my_team_name if match_info else ""

    def recording_cameras(self) -> set[str] | None:
        """Cameras that recorded this group, from discovery-time metadata.

        None when any recording carries no ``camera_name`` (discovered before
        attribution existed): then it could be any camera's, and the caller
        must treat every configured camera as involved.
        """
        names: set[str] = set()
        for rec in DirectoryState(self.group_dir).files.values():
            metadata = getattr(rec, "metadata", None) or {}
            owner = metadata.get("camera_name") if isinstance(metadata, dict) else None
            if not owner:
                return None
            names.add(str(owner))
        return names or None

    def destination(self, team_root: str) -> str:
        """Where this group's archive copy belongs, inside its team's root.

        ``team_root`` is already team-specific (``archive_path`` in its ``[TEAM.<key>]``) —
        no team component is appended here. The root name cannot be derived
        from the team name anyway: a team recorded as "Guzzetta" archives to
        ``Heat_2012s``, and one account can hold several age groups that must
        not be mixed.

        The game folder is named from ``match_info.ini`` rather than the group
        directory, which is only a camera timestamp:

            <team_root>/2026.07.12 - vs Niagara Falls Soccer Club (away)

        Falls back to the raw group name when match info is unusable, so a
        game is never archived to a path built from empty strings.
        """
        group_name = self._group_name()
        match_info = MatchInfo.from_file(os.path.join(self.group_dir, _MATCH_INFO))
        if match_info is None or not match_info.opponent_team_name:
            logger.warning(
                "ARCHIVE: %s has no usable match_info; archiving under its "
                "raw group name.",
                group_name,
            )
            return os.path.join(team_root, group_name)

        date_part = group_name[:10]
        home_away = "home" if match_info.location.strip().lower() == "home" else "away"
        name = (
            f"{date_part} - vs "
            f"{_safe_component(match_info.opponent_team_name)} ({home_away})"
        )
        return os.path.join(team_root, name)

    def _group_name(self) -> str:
        return os.path.basename(self.group_dir.rstrip("\\/"))

    def _read_marker_owner(self, folder: str) -> str | None:
        """The source group a folder's marker names, or None if unclaimed."""
        try:
            with open(os.path.join(folder, ARCHIVE_MARKER), encoding="utf-8") as fh:
                owner = json.load(fh).get("source_group")
        except (OSError, ValueError, AttributeError):
            return None
        return str(owner) if owner else None

    def _write_marker(self, folder: str) -> None:
        marker = os.path.join(folder, ARCHIVE_MARKER)
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "source_group": self._group_name(),
                    "source_path": self.group_dir,
                    "claimed_at": datetime.now().isoformat(timespec="seconds"),
                },
                fh,
            )
        os.replace(tmp, marker)

    def claim_destination(self, team_root: str) -> str:
        """The archive folder this group owns, claiming one if needed.

        Starts from :meth:`destination` and walks " (2)", " (3)" ... until it
        finds a folder this group already owns (a resumed run) or one that is
        free. A folder whose marker names another group -- or that has no
        marker but holds files, like one an earlier hand-run archive left --
        is never written into: copying a second game over the first game's
        only copy is unrecoverable. An empty, unmarked folder (this group's
        own run, interrupted between creating it and writing the marker) is
        claimed. Raises when no folder can be claimed; never guesses.
        """
        base = self.destination(team_root)
        mine = self._group_name()
        for n in range(1, _MAX_DESTINATION_SUFFIX + 1):
            candidate = base if n == 1 else f"{base} ({n})"
            if not os.path.exists(candidate):
                os.makedirs(candidate, exist_ok=True)
                self._write_marker(candidate)
                return candidate
            if not os.path.isdir(candidate):
                continue
            owner = self._read_marker_owner(candidate)
            if owner == mine:
                return candidate
            if owner is None and not os.listdir(candidate):
                self._write_marker(candidate)
                return candidate
        raise RuntimeError(
            f"ARCHIVE: no free archive folder for {mine} under {team_root!r} "
            f"after {_MAX_DESTINATION_SUFFIX} tries of {os.path.basename(base)!r}."
        )

    async def execute(
        self,
        archive_root: str = "",
        make_second_copy: bool = True,
        reclaim_local_space: bool = True,
        watermark: datetime | None = None,
    ) -> bool:
        """Run the archive. Returns True when the group has been dealt with.

        The two flags are independent, matching ``[ARCHIVE] after_upload``:
        keep a second copy at ``archive_root``, and/or reclaim the local
        files. An install with a single drive sets no ``archive_root`` at all
        and still reclaims space -- YouTube is its archive.

        Only a ``complete`` group (published) is archived, or an ``archived``
        one whose reclaim did not finish. Anything else is refused: copying an
        unfinished group and recording it ``archived`` would end its pipeline,
        and reclaiming it would delete footage that was never published.

        ``watermark`` is the camera poller's high-water mark for the cameras
        that recorded this group. Reclaiming a group the watermark has not yet
        passed is unsafe: the poller's query window still covers those
        recordings, and every "do we have this?" test it can run --
        ``is_file_in_state`` against the group's ``state.json``, or a stat of
        the ``.mp4`` -- reads the local disk we are about to empty. It would
        see the game as new and download it again. So this is a hard refusal,
        not a warning: the task fails, is retried later, and succeeds once the
        watermark catches up. A group with no recording end time cannot be
        checked against the watermark at all, so it is refused too.

        Raises ``ValueError`` when ``group_dir`` is relative or missing.
        """
        self.require_real_group_dir()
        group = self._group_name()

        if make_second_copy and not archive_root:
            logger.error("ARCHIVE: no archive path configured; refusing to run.")
            return False

        dir_state = DirectoryState(self.group_dir)
        if dir_state.status == "archived":
            if not reclaim_local_space:
                logger.info("ARCHIVE: %s already archived; nothing to do.", group)
                return True
            # `archived` is recorded before any delete. Local files still here
            # mean a reclaim that did not finish (a locked file, a crash).
            # Finish it -- through copy + verify again, since `archived` alone
            # does not prove the copy is still intact.
            logger.info(
                "ARCHIVE: %s is archived but still on local disk; finishing "
                "the reclaim.",
                group,
            )
        elif dir_state.status != "complete":
            logger.error(
                "ARCHIVE: refusing %s -- its status is %r, not 'complete'. Only "
                "a published game is archived; anything earlier is still in "
                "the pipeline.",
                group,
                dir_state.status,
            )
            return False

        if reclaim_local_space:
            newest = max(
                (
                    f.end_time
                    for f in dir_state.files.values()
                    if isinstance(getattr(f, "end_time", None), datetime)
                ),
                default=None,
            )
            if newest is None:
                logger.error(
                    "ARCHIVE: refusing to reclaim %s -- none of its recordings "
                    "has an end time, so it cannot be checked against the "
                    "camera watermark.",
                    group,
                )
                return False
            if watermark is None or watermark < newest:
                logger.error(
                    "ARCHIVE: refusing to reclaim %s -- the camera watermark "
                    "(%s) has not passed its newest recording (%s). Deleting "
                    "now would let the poller rediscover this game as new and "
                    "re-download it. Will retry once the watermark advances.",
                    group,
                    watermark,
                    newest,
                )
                return False

            if not make_second_copy:
                # `discard`: the local files are the only copy we hold, so
                # YouTube has to actually have the game before we drop them.
                # The group reaching `complete` is not proof on its own --
                # recorded video ids are.
                uploaded = dir_state.get_uploaded_videos()
                if not uploaded:
                    logger.error(
                        "ARCHIVE: refusing to discard %s -- no uploaded video "
                        "id recorded for it, so YouTube is not known to hold a "
                        "copy and these are the only files that exist.",
                        group,
                    )
                    return False
                logger.info(
                    "ARCHIVE: %s is on YouTube (%s); reclaiming local files "
                    "without a second copy.",
                    group,
                    ", ".join(f"{k}={v}" for k, v in sorted(uploaded.items())),
                )

        # ---- copy + verify ------------------------------------------------
        # `verified` doubles as the delete list, so for `discard` (no second
        # copy) it is simply every file in the group.
        verified: list[str] = []
        if not make_second_copy:
            for root, _dirs, files in os.walk(self.group_dir):
                verified.extend(os.path.join(root, name) for name in files)
            return await self._finalize(dir_state, verified, reclaim_local_space, "")

        dest = self.claim_destination(archive_root)
        for root, _dirs, files in os.walk(self.group_dir):
            for name in files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, self.group_dir)
                dst = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)

                # `dest` is this group's own folder (claim_destination), so
                # anything already at `dst` is a leftover of our own
                # interrupted run and may be overwritten.
                if os.path.exists(dst):
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

                src_hash = _sha256(src)
                if src_hash != _sha256(dst):
                    # A torn write from an interrupted run has the right size
                    # and the wrong bytes. Re-copy once; a second mismatch is
                    # a real problem (failing media, a file changing under us).
                    logger.warning(
                        "ARCHIVE: SHA-256 mismatch for %s; re-copying once.", rel
                    )
                    shutil.copy2(src, dst)
                    if src_hash != _sha256(dst):
                        logger.error(
                            "ARCHIVE: SHA-256 mismatch for %s persists after a "
                            "re-copy; aborting %s. Nothing deleted.",
                            rel,
                            group,
                        )
                        return False
                verified.append(src)

        logger.info(
            "ARCHIVE: verified %d file(s) of %s -> %s",
            len(verified),
            group,
            dest,
        )
        return await self._finalize(dir_state, verified, reclaim_local_space, dest)

    async def _finalize(
        self,
        dir_state: DirectoryState,
        verified: list[str],
        reclaim_local_space: bool,
        dest: str,
    ) -> bool:
        """Record the group as archived, then optionally reclaim its files.

        Shared by every disposition so the ordering can only be written once.
        See the module docstring: recording BEFORE deleting is what makes an
        interrupted run recoverable instead of a game that reprocesses itself.
        """
        group = self._group_name()
        if dir_state.status != "archived":
            await dir_state.update_group_status("archived")

        if not reclaim_local_space:
            logger.info("ARCHIVE: %s copied to %s; local files kept.", group, dest)
            return True

        # Delete by explicit path, only files that were accounted for.
        # match_info.ini goes after everything else and state.json last of
        # all, so an interrupted reclaim still reads as archived AND still
        # resolves to the same archive folder when it is retried.
        state_file = os.path.abspath(dir_state.state_file_path)
        match_info = os.path.abspath(os.path.join(self.group_dir, _MATCH_INFO))
        ordered = sorted(
            (p for p in verified if os.path.abspath(p) != state_file),
            key=lambda p: os.path.abspath(p) == match_info,
        )
        for path in ordered:
            try:
                os.remove(path)
            except OSError as e:
                logger.error(
                    "ARCHIVE: could not remove %s: %s. %s stays archived; the "
                    "retry finishes the reclaim.",
                    path,
                    e,
                    group,
                )
                return False

        try:
            if os.path.exists(state_file):
                os.remove(state_file)
            shutil.rmtree(self.group_dir, ignore_errors=True)
        except OSError as e:
            logger.error(
                "ARCHIVE: archived %s but could not fully remove it: %s",
                group,
                e,
            )
            return False

        logger.info(
            "ARCHIVE: %s reclaimed locally%s.",
            group,
            f" (archived to {dest})" if dest else " (YouTube holds the copy)",
        )
        return True
