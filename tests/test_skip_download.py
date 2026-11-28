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
