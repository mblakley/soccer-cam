"""A person giving up on a download that keeps failing.

``skip_download`` is the one function the dashboard's Skip button calls (and
the TTT command will call): it settles a stuck file as ``skipped`` so the
camera watermark can pass it. These tests pin what it accepts, what it
refuses, and that the decision survives the rest of the pipeline writing
state.json around it. Where ``skipped`` is honoured downstream (watermark,
auditor, archive guard, download processor) is tested next to each of those.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.models.directory_state import (
    SETTLED_FILE_STATUSES,
    SETTLED_GROUP_STATUSES,
    SKIPPED_GROUP_STATUS,
    SkipDownloadError,
    list_stuck_downloads,
    skip_download,
)

GROUP = "2026.07.01-10.00.00"


async def _group(storage, files, *, name=GROUP, group_status=None):
    """A group dir whose state.json holds ``files`` as (basename, status, extra).

    Path.mkdir, not os.makedirs -- conftest patches the latter to a no-op.
    """
    group_dir = os.path.join(storage, name)
    Path(group_dir).mkdir(parents=True, exist_ok=True)
    dir_state = DirectoryState(group_dir)
    start = datetime(2026, 7, 1, 10, 0, 0)
    for i, (basename, status, extra) in enumerate(files):
        path = os.path.join(group_dir, basename)
        rec = RecordingFile(
            start_time=start + timedelta(minutes=30 * i),
            end_time=start + timedelta(minutes=30 * (i + 1)),
            file_path=path,
            status=status,
            metadata={"path": f"/mnt/sd/{basename}", "camera_name": "Field cam"},
        )
        for key, value in (extra or {}).items():
            setattr(rec, key, value)
        await dir_state.add_file(path, rec)
    if group_status:
        await dir_state.update_group_status(group_status)
    return group_dir


def _file(group_dir, basename):
    return DirectoryState(group_dir).files[os.path.join(group_dir, basename)]


class TestSkipDownload:
    @pytest.mark.asyncio
    async def test_skips_a_failing_file_and_records_who_when_why(self, temp_storage):
        group_dir = await _group(temp_storage, [("bad.mp4", "download_failed", None)])

        result = skip_download(
            temp_storage, GROUP, "bad.mp4", by="dashboard", reason="corrupt on SD"
        )

        assert result.changed is True
        assert result.previous_status == "download_failed"
        rec = _file(group_dir, "bad.mp4")
        assert rec.status == "skipped"
        assert rec.status in SETTLED_FILE_STATUSES
        assert rec.skip is True
        assert rec.skipped_by == "dashboard"
        assert rec.skip_reason == "corrupt on SD"
        assert datetime.fromisoformat(rec.skipped_at)

    @pytest.mark.asyncio
    async def test_accepts_full_paths_for_group_and_file(self, temp_storage):
        """The TTT command may hand over paths rather than names."""
        group_dir = await _group(temp_storage, [("bad.mp4", "download_failed", None)])

        skip_download(
            temp_storage,
            group_dir,
            os.path.join(group_dir, "bad.mp4"),
            by="ttt:coach@example.com",
        )

        assert _file(group_dir, "bad.mp4").status == "skipped"

    @pytest.mark.asyncio
    async def test_only_the_named_file_changes(self, temp_storage):
        group_dir = await _group(
            temp_storage,
            [("bad.mp4", "download_failed", None), ("ok.mp4", "downloaded", None)],
        )

        skip_download(temp_storage, GROUP, "bad.mp4", by="dashboard")

        assert _file(group_dir, "ok.mp4").status == "downloaded"
        assert _file(group_dir, "ok.mp4").skip is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["downloaded", "combined", "complete"])
    async def test_refuses_a_file_that_already_downloaded(self, temp_storage, status):
        """Skip is for downloads that keep failing. A file we have the bytes
        for has nothing to give up -- and marking it skipped would drop it
        from its game's combine."""
        group_dir = await _group(temp_storage, [("good.mp4", status, None)])

        with pytest.raises(SkipDownloadError, match="already"):
            skip_download(temp_storage, GROUP, "good.mp4", by="dashboard")

        rec = _file(group_dir, "good.mp4")
        assert rec.status == status
        assert rec.skip is False

    @pytest.mark.asyncio
    async def test_refuses_a_file_that_is_downloading_now(self, temp_storage):
        await _group(temp_storage, [("busy.mp4", "downloading", None)])

        with pytest.raises(SkipDownloadError, match="downloading right now"):
            skip_download(temp_storage, GROUP, "busy.mp4", by="dashboard")

    @pytest.mark.asyncio
    async def test_refuses_a_group_already_past_download(self, temp_storage):
        await _group(
            temp_storage,
            [("stale.mp4", "download_failed", None)],
            group_status="combined",
        )

        with pytest.raises(SkipDownloadError, match="already combined"):
            skip_download(temp_storage, GROUP, "stale.mp4", by="dashboard")

    @pytest.mark.asyncio
    async def test_unknown_file_hard_fails(self, temp_storage):
        await _group(temp_storage, [("bad.mp4", "download_failed", None)])

        with pytest.raises(SkipDownloadError, match="not a recording"):
            skip_download(temp_storage, GROUP, "nope.mp4", by="dashboard")

    @pytest.mark.parametrize(
        "group",
        [
            "2026.07.02-10.00.00",  # well-formed, but no such group
            "not-a-group",  # not a name the poller makes
            "../2026.07.01-10.00.00",  # escapes the storage root
            "",
        ],
        ids=["missing", "malformed", "traversal", "empty"],
    )
    def test_unknown_or_foreign_group_hard_fails(self, temp_storage, group):
        with pytest.raises(SkipDownloadError):
            skip_download(temp_storage, group, "bad.mp4", by="dashboard")

    @pytest.mark.asyncio
    async def test_who_is_required(self, temp_storage):
        group_dir = await _group(temp_storage, [("bad.mp4", "download_failed", None)])

        with pytest.raises(SkipDownloadError, match="who"):
            skip_download(temp_storage, GROUP, "bad.mp4", by="  ")

        assert _file(group_dir, "bad.mp4").status == "download_failed"

    @pytest.mark.asyncio
    async def test_re_skip_is_a_no_op_that_keeps_the_first_record(self, temp_storage):
        group_dir = await _group(temp_storage, [("bad.mp4", "download_failed", None)])
        skip_download(temp_storage, GROUP, "bad.mp4", by="dashboard", reason="first")
        first = _file(group_dir, "bad.mp4")

        again = skip_download(temp_storage, GROUP, "bad.mp4", by="ttt", reason="2nd")

        assert again.changed is False
        rec = _file(group_dir, "bad.mp4")
        assert (rec.skipped_at, rec.skipped_by, rec.skip_reason) == (
            first.skipped_at,
            "dashboard",
            "first",
        )

    @pytest.mark.asyncio
    async def test_abandoned_file_can_be_skipped(self, temp_storage):
        """A person may put their name on a give-up the downloader made."""
        group_dir = await _group(temp_storage, [("gone.mp4", "abandoned", None)])

        assert skip_download(temp_storage, GROUP, "gone.mp4", by="dashboard").changed
        assert _file(group_dir, "gone.mp4").status == "skipped"

    @pytest.mark.asyncio
    async def test_a_stale_in_memory_state_cannot_un_skip(self, temp_storage):
        """The download processor holds a DirectoryState for the length of a
        download and saves the whole file map. A skip made in the meantime
        must survive that save, or the person's decision silently reverts."""
        group_dir = await _group(
            temp_storage,
            [("bad.mp4", "download_failed", None), ("next.mp4", "pending", None)],
        )
        stale = DirectoryState(group_dir)  # loaded before the skip

        skip_download(temp_storage, GROUP, "bad.mp4", by="dashboard")
        await stale.update_file_state(
            os.path.join(group_dir, "next.mp4"), status="downloaded"
        )

        assert _file(group_dir, "bad.mp4").status == "skipped"
        assert _file(group_dir, "bad.mp4").skipped_by == "dashboard"
        assert _file(group_dir, "next.mp4").status == "downloaded"


class TestListStuckDownloads:
    @pytest.mark.asyncio
    async def test_lists_failed_files_with_what_a_person_needs(self, temp_storage):
        await _group(
            temp_storage,
            [
                (
                    "bad.mp4",
                    "download_failed",
                    {
                        "failed_attempts": 4,
                        "error_message": "HTTP 500",
                        "last_failed_at": "2026-07-02T08:00:00",
                    },
                ),
                ("ok.mp4", "downloaded", None),
                ("busy.mp4", "downloading", {"failed_attempts": 2}),
                ("retrying.mp4", "pending", {"failed_attempts": 1}),
                ("fresh.mp4", "pending", None),
                ("done.mp4", "skipped", None),
                ("gone.mp4", "abandoned", None),
            ],
        )

        stuck = list_stuck_downloads(temp_storage)

        assert [s["file_name"] for s in stuck] == ["bad.mp4", "retrying.mp4"]
        bad = stuck[0]
        assert bad["group"] == GROUP
        assert bad["camera"] == "Field cam"
        assert bad["failed_attempts"] == 4
        assert bad["last_error"] == "HTTP 500"
        assert bad["start_time"] == "2026-07-01T10:00:00"

    @pytest.mark.asyncio
    async def test_group_past_download_is_not_listed(self, temp_storage):
        await _group(
            temp_storage,
            [("stale.mp4", "download_failed", None)],
            group_status="combined",
        )

        assert list_stuck_downloads(temp_storage) == []

    def test_unreadable_state_is_passed_over(self, temp_storage):
        bad = Path(temp_storage) / GROUP
        bad.mkdir()
        (bad / "state.json").write_text("{not json", encoding="utf-8")
        (Path(temp_storage) / "notes").mkdir()

        assert list_stuck_downloads(temp_storage) == []

    @pytest.mark.asyncio
    async def test_skipped_file_leaves_the_list(self, temp_storage):
        await _group(temp_storage, [("bad.mp4", "download_failed", None)])
        assert len(list_stuck_downloads(temp_storage)) == 1

        skip_download(temp_storage, GROUP, "bad.mp4", by="dashboard")

        assert list_stuck_downloads(temp_storage) == []
        state = json.loads(
            (Path(temp_storage) / GROUP / "state.json").read_text(encoding="utf-8")
        )
        assert next(iter(state["files"].values()))["status"] == "skipped"


class TestSkippedFilesStayOutOfTheGame:
    """A skipped file is out of the game, even with bytes left on disk (a
    download that stopped short leaves its partial file behind)."""

    @pytest.mark.asyncio
    async def test_combine_leaves_out_a_skipped_file(self, temp_storage):
        from video_grouper.task_processors.tasks.video.combine_task import (
            CombineTask,
        )

        group_dir = await _group(
            temp_storage,
            [
                ("c1.dav", "downloaded", None),
                ("c2.dav", "download_failed", None),
                ("c3.dav", "downloaded", None),
            ],
        )
        for name in ("c1.dav", "c2.dav", "c3.dav"):
            Path(group_dir, name).write_bytes(b"x")
        skip_download(temp_storage, GROUP, "c2.dav", by="dashboard")

        task = CombineTask(group_dir=group_dir)
        task.storage_path = temp_storage  # set by the queue processor
        names = [os.path.basename(p) for p in task.get_dav_files()]
        assert names == ["c1.dav", "c3.dav"]

    @pytest.mark.asyncio
    async def test_corrupt_recovery_rebuilds_without_a_skipped_file(self, temp_storage):
        from video_grouper.task_processors.corrupt_recovery import _source_segments

        group_dir = await _group(
            temp_storage,
            [("c1.dav", "downloaded", None), ("c2.dav", "download_failed", None)],
        )
        for name in ("c1.dav", "c2.dav"):
            Path(group_dir, name).write_bytes(b"x")
        skip_download(temp_storage, GROUP, "c2.dav", by="dashboard")

        assert [os.path.basename(p) for p in _source_segments(group_dir)] == ["c1.dav"]

    @pytest.mark.asyncio
    async def test_the_first_file_is_the_first_one_not_skipped(self, temp_storage):
        """It is the game's video time 0: a skipped first file is not in the
        combined video."""
        group_dir = await _group(
            temp_storage,
            [("f1.dav", "download_failed", None), ("f2.dav", "downloaded", None)],
        )
        skip_download(temp_storage, GROUP, "f1.dav", by="dashboard")
        first = DirectoryState(group_dir).get_first_file()
        assert first is not None
        assert os.path.basename(first.file_path) == "f2.dav"

    @pytest.mark.asyncio
    async def test_with_every_file_skipped_the_first_file_is_still_named(
        self, temp_storage
    ):
        """Callers that only read the camera's name from it keep working."""
        group_dir = await _group(temp_storage, [("f1.dav", "download_failed", None)])
        skip_download(temp_storage, GROUP, "f1.dav", by="dashboard")
        first = DirectoryState(group_dir).get_first_file()
        assert first is not None
        assert os.path.basename(first.file_path) == "f1.dav"


class TestEveryFileSkippedSettlesTheGroup:
    """A group whose every file was skipped has nothing to combine. It must
    settle (``skipped``) rather than sit in the download phase forever."""

    @pytest.mark.asyncio
    async def test_skipping_the_last_remaining_file_settles_the_group(
        self, temp_storage
    ):
        group_dir = await _group(
            temp_storage,
            [("a.dav", "download_failed", None), ("b.dav", "download_failed", None)],
        )

        skip_download(temp_storage, GROUP, "a.dav", by="dashboard")
        assert DirectoryState(group_dir).status not in SETTLED_GROUP_STATUSES

        skip_download(temp_storage, GROUP, "b.dav", by="dashboard")

        state = DirectoryState(group_dir)
        assert state.status == SKIPPED_GROUP_STATUS
        assert SKIPPED_GROUP_STATUS in SETTLED_GROUP_STATUSES
        assert state.is_ready_for_combining() is False
        # Nothing is deleted: both entries are still recorded, as skipped.
        assert {f.status for f in state.files.values()} == {"skipped"}

    @pytest.mark.asyncio
    async def test_a_skipped_group_is_no_longer_listed_as_stuck(self, temp_storage):
        await _group(temp_storage, [("a.dav", "download_failed", None)])
        skip_download(temp_storage, GROUP, "a.dav", by="dashboard")
        assert list_stuck_downloads(temp_storage) == []

    @pytest.mark.asyncio
    async def test_a_stale_in_memory_state_cannot_unsettle_it(self, temp_storage):
        """A DirectoryState loaded before the last skip saves its own (stale)
        group status with the file map; the settled status must survive."""
        group_dir = await _group(temp_storage, [("a.dav", "download_failed", None)])
        stale = DirectoryState(group_dir)

        skip_download(temp_storage, GROUP, "a.dav", by="dashboard")
        await stale.save_state()

        assert DirectoryState(group_dir).status == SKIPPED_GROUP_STATUS

    @pytest.mark.asyncio
    async def test_one_skipped_one_downloaded_still_combines(self, temp_storage):
        group_dir = await _group(
            temp_storage,
            [("a.dav", "download_failed", None), ("b.dav", "downloaded", None)],
        )
        Path(group_dir, "b.dav").write_bytes(b"x")

        skip_download(temp_storage, GROUP, "a.dav", by="dashboard")

        state = DirectoryState(group_dir)
        assert state.status != SKIPPED_GROUP_STATUS
        assert state.is_ready_for_combining() is True


@pytest.mark.asyncio
async def test_a_reason_over_200_characters_is_refused(temp_storage):
    """The form caps it; the TTT command and a hand-made POST must not get
    past that."""
    await _group(temp_storage, [("a.dav", "download_failed", None)])
    with pytest.raises(SkipDownloadError):
        skip_download(temp_storage, GROUP, "a.dav", by="dashboard", reason="x" * 201)
    skip_download(temp_storage, GROUP, "a.dav", by="dashboard", reason="x" * 200)


@pytest.mark.asyncio
async def test_a_new_file_reopens_a_skipped_group_at_once(temp_storage):
    """The poller adds a file to an all-skipped group and, in the same poll,
    advances the watermark. If the group still read ``skipped`` (a settled
    status) the watermark would pass the new file before it downloads."""
    from video_grouper.models.directory_state import SKIPPED_GROUP_STATUS

    group_dir = await _group(temp_storage, [("a.dav", "download_failed", None)])
    skip_download(temp_storage, GROUP, "a.dav", by="dashboard")
    assert DirectoryState(group_dir).status == SKIPPED_GROUP_STATUS

    state = DirectoryState(group_dir)
    path = os.path.join(group_dir, "b.dav")
    start = datetime(2026, 7, 1, 11, 0, 0)
    await state.add_file(
        path,
        RecordingFile(
            start_time=start,
            end_time=start + timedelta(minutes=30),
            file_path=path,
            status="pending",
            metadata={"path": "/mnt/sd/b.dav", "camera_name": "Field cam"},
        ),
    )
    assert DirectoryState(group_dir).status == "pending"
