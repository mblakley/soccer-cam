"""Tests for ClipRequestProcessor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_grouper.task_processors.clip_request_processor import ClipRequestProcessor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(folder_id: str | None = "folder-abc"):
    """Minimal Config stand-in exposing the ttt attributes the processor reads."""
    ttt = MagicMock()
    ttt.google_drive_folder_id = folder_id
    config = MagicMock()
    config.ttt = ttt
    return config


def _make_processor(
    tmp_path,
    *,
    ttt_client=None,
    drive_uploader=None,
    youtube_uploader=None,
    ntfy_service=None,
    folder_id: str | None = "folder-abc",
):
    if ttt_client is None:
        ttt_client = MagicMock()
        ttt_client.is_authenticated = MagicMock(return_value=True)
    drive_uploader = drive_uploader or MagicMock()
    return ClipRequestProcessor(
        storage_path=str(tmp_path),
        config=_make_config(folder_id=folder_id),
        ttt_client=ttt_client,
        drive_uploader=drive_uploader,
        ntfy_service=ntfy_service,
        youtube_uploader=youtube_uploader,
        poll_interval=60,
    )


def _make_request(
    *,
    req_id: str = "req-00000001-aaaa-bbbb-cccc-000000000000",
    delivery_method: str = "external_storage",
    is_compilation: bool = False,
    segments: list | None = None,
    recording_group_dir: str = "game-2026-04-01",
    opponent: str = "IYSA",
    start_time: str = "2026-04-01T10:00:00Z",
    notes: str | None = None,
):
    if segments is None:
        segments = [{"start_time": 100, "end_time": 130, "label": "goal"}]
    return {
        "id": req_id,
        "delivery_method": delivery_method,
        "is_compilation": is_compilation,
        "segments": segments,
        "notes": notes,
        "game_session": {
            "recording_group_dir": recording_group_dir,
            "opponent_name": opponent,
            "start_time": start_time,
        },
    }


# ---------------------------------------------------------------------------
# _resolve_recording_dir
# ---------------------------------------------------------------------------


class TestResolveRecordingDir:
    def test_absolute_path_exists(self, tmp_path):
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        proc = _make_processor(tmp_path)
        req = {"id": "r1", "game_session": {"recording_group_dir": str(game_dir)}}
        assert proc._resolve_recording_dir(req) == str(game_dir)

    def test_relative_to_storage(self, tmp_path):
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        proc = _make_processor(tmp_path)
        req = {"id": "r1", "game_session": {"recording_group_dir": "game-x"}}
        assert proc._resolve_recording_dir(req) == str(game_dir)

    def test_not_found_returns_none(self, tmp_path):
        proc = _make_processor(tmp_path)
        req = {"id": "r1", "game_session": {"recording_group_dir": "missing"}}
        assert proc._resolve_recording_dir(req) is None

    def test_no_recording_group_dir(self, tmp_path):
        proc = _make_processor(tmp_path)
        assert proc._resolve_recording_dir({"id": "r1", "game_session": {}}) is None


# ---------------------------------------------------------------------------
# _find_source_video
# ---------------------------------------------------------------------------


class TestFindSourceVideo:
    def test_direct_file(self, tmp_path):
        game_dir = tmp_path / "g"
        game_dir.mkdir()
        combined = game_dir / "combined.mp4"
        combined.write_bytes(b"\x00")
        proc = _make_processor(tmp_path)
        assert proc._find_source_video(str(game_dir)) == str(combined)

    def test_subdirectory(self, tmp_path):
        game_dir = tmp_path / "g"
        game_dir.mkdir()
        sub = game_dir / "recording-2026.04.01"
        sub.mkdir()
        combined = sub / "combined.mp4"
        combined.write_bytes(b"\x00")
        proc = _make_processor(tmp_path)
        assert proc._find_source_video(str(game_dir)) == str(combined)

    def test_not_found(self, tmp_path):
        game_dir = tmp_path / "g"
        game_dir.mkdir()
        proc = _make_processor(tmp_path)
        assert proc._find_source_video(str(game_dir)) is None


# ---------------------------------------------------------------------------
# _extract_segments
# ---------------------------------------------------------------------------


class TestExtractSegments:
    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        proc = _make_processor(tmp_path)
        segments = [
            {"start_time": 0, "end_time": 10, "label": "a", "sort_order": 0},
            {"start_time": 50, "end_time": 80, "label": "b", "sort_order": 1},
        ]
        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ) as mock_extract:
            paths = await proc._extract_segments(
                "/src/combined.mp4", segments, "req12345"
            )
        assert len(paths) == 2
        assert mock_extract.await_count == 2

    @pytest.mark.asyncio
    async def test_invalid_times_skipped(self, tmp_path):
        proc = _make_processor(tmp_path)
        segments = [
            {"start_time": 10, "end_time": 5},  # end <= start
            {"start_time": 0, "end_time": 0},  # zero duration
            {"start_time": 5, "end_time": 15, "label": "good"},
        ]
        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ) as mock_extract:
            paths = await proc._extract_segments(
                "/src/combined.mp4", segments, "req12345"
            )
        assert len(paths) == 1
        assert mock_extract.await_count == 1

    @pytest.mark.asyncio
    async def test_extract_failure_continues(self, tmp_path):
        proc = _make_processor(tmp_path)
        segments = [
            {"start_time": 0, "end_time": 10, "label": "a"},
            {"start_time": 20, "end_time": 30, "label": "b"},
        ]
        results = [RuntimeError("boom"), "ok"]

        async def fake_extract(*args, **kwargs):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("video_grouper.utils.ffmpeg_utils.extract_clip", new=fake_extract):
            paths = await proc._extract_segments(
                "/src/combined.mp4", segments, "req12345"
            )
        assert len(paths) == 1

    @pytest.mark.asyncio
    async def test_sort_order_respected(self, tmp_path):
        proc = _make_processor(tmp_path)
        segments = [
            {"start_time": 50, "end_time": 60, "label": "second", "sort_order": 1},
            {"start_time": 0, "end_time": 10, "label": "first", "sort_order": 0},
        ]
        call_order = []

        async def fake_extract(src, start, end, out):
            call_order.append(start)
            return out

        with patch("video_grouper.utils.ffmpeg_utils.extract_clip", new=fake_extract):
            await proc._extract_segments("/src/combined.mp4", segments, "req12345")
        assert call_order == [0, 50]


# ---------------------------------------------------------------------------
# _process_request
# ---------------------------------------------------------------------------


class TestProcessRequestDispatch:
    @pytest.mark.asyncio
    async def test_external_storage_single_segment(self, tmp_path):
        """external_storage with one segment uploads once to Drive, fulfills with the URL."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ttt.start_clip_request = MagicMock()
        ttt.fulfill_clip_request = MagicMock()

        drive = MagicMock()
        drive.upload_and_share = MagicMock(return_value="https://drive.example/view/1")

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(
            delivery_method="external_storage",
            recording_group_dir="game-x",
            segments=[{"start_time": 0, "end_time": 10, "label": "g1"}],
        )

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        ttt.start_clip_request.assert_called_once_with(req["id"])
        drive.upload_and_share.assert_called_once()
        ttt.fulfill_clip_request.assert_called_once()
        assert (
            ttt.fulfill_clip_request.call_args.args[1] == "https://drive.example/view/1"
        )

    @pytest.mark.asyncio
    async def test_external_storage_compilation(self, tmp_path):
        """external_storage + is_compilation merges multi-segment clips to a single Drive upload."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()
        drive.upload_and_share = MagicMock(
            return_value="https://drive.example/view/comp"
        )

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(
            delivery_method="external_storage",
            recording_group_dir="game-x",
            is_compilation=True,
            segments=[
                {"start_time": 0, "end_time": 10, "label": "a"},
                {"start_time": 20, "end_time": 30, "label": "b"},
                {"start_time": 40, "end_time": 50, "label": "c"},
            ],
        )

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "video_grouper.utils.ffmpeg_utils.compile_clips",
                new=AsyncMock(return_value="compiled"),
            ) as mock_compile,
        ):
            await proc._process_request(req)

        mock_compile.assert_awaited_once()
        # Single upload after compilation
        assert drive.upload_and_share.call_count == 1
        ttt.fulfill_clip_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_external_storage_multi_no_compilation(self, tmp_path):
        """external_storage without is_compilation uploads N files with joined URL."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()
        urls = iter(["u1", "u2"])
        drive.upload_and_share = MagicMock(side_effect=lambda *a, **k: next(urls))

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(
            delivery_method="external_storage",
            recording_group_dir="game-x",
            is_compilation=False,
            segments=[
                {"start_time": 0, "end_time": 10, "label": "a"},
                {"start_time": 20, "end_time": 30, "label": "b"},
            ],
        )

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        assert drive.upload_and_share.call_count == 2
        fulfilled_url = ttt.fulfill_clip_request.call_args.args[1]
        assert fulfilled_url == "u1, u2"

    @pytest.mark.asyncio
    async def test_youtube_compilation(self, tmp_path):
        """delivery_method=youtube + multi-segment → compile → single YT upload → youtu.be URL."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        yt = MagicMock()
        yt.upload_video = MagicMock(return_value="abc123")

        proc = _make_processor(tmp_path, ttt_client=ttt, youtube_uploader=yt)
        req = _make_request(
            delivery_method="youtube",
            recording_group_dir="game-x",
            is_compilation=True,
            segments=[
                {"start_time": 0, "end_time": 10, "label": "a"},
                {"start_time": 20, "end_time": 30, "label": "b"},
            ],
        )

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "video_grouper.utils.ffmpeg_utils.compile_clips",
                new=AsyncMock(return_value="compiled"),
            ) as mock_compile,
        ):
            await proc._process_request(req)

        mock_compile.assert_awaited_once()
        yt.upload_video.assert_called_once()
        fulfilled_url = ttt.fulfill_clip_request.call_args.args[1]
        assert fulfilled_url == "https://youtu.be/abc123"

    @pytest.mark.asyncio
    async def test_youtube_single_segment_skips_compile(self, tmp_path):
        """YouTube with one segment uploads directly without compilation."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        yt = MagicMock()
        yt.upload_video = MagicMock(return_value="xyz999")

        proc = _make_processor(tmp_path, ttt_client=ttt, youtube_uploader=yt)
        req = _make_request(
            delivery_method="youtube",
            recording_group_dir="game-x",
            segments=[{"start_time": 0, "end_time": 10, "label": "g1"}],
        )

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "video_grouper.utils.ffmpeg_utils.compile_clips",
                new=AsyncMock(return_value="compiled"),
            ) as mock_compile,
        ):
            await proc._process_request(req)

        mock_compile.assert_not_awaited()
        yt.upload_video.assert_called_once()
        assert ttt.fulfill_clip_request.call_args.args[1] == "https://youtu.be/xyz999"

    @pytest.mark.asyncio
    async def test_youtube_without_uploader_fails_cleanly(self, tmp_path):
        """delivery_method=youtube but no uploader → no fulfill, no crash."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)

        proc = _make_processor(tmp_path, ttt_client=ttt, youtube_uploader=None)
        req = _make_request(delivery_method="youtube", recording_group_dir="game-x")

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        ttt.fulfill_clip_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_footage_notifies(self, tmp_path):
        """No combined.mp4 → ntfy notify, no TTT state change."""
        (tmp_path / "game-x").mkdir()  # dir exists but no combined.mp4

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ntfy = MagicMock()
        ntfy.send_notification = AsyncMock()

        proc = _make_processor(tmp_path, ttt_client=ttt, ntfy_service=ntfy)
        req = _make_request(recording_group_dir="game-x")

        await proc._process_request(req)

        ttt.start_clip_request.assert_not_called()
        ttt.fulfill_clip_request.assert_not_called()
        # Let the background notification task run
        await asyncio.sleep(0)
        ntfy.send_notification.assert_called()

    @pytest.mark.asyncio
    async def test_already_in_progress_continues(self, tmp_path):
        """If start_clip_request says 'Cannot start' (already in_progress), processing continues."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ttt.start_clip_request = MagicMock(
            side_effect=RuntimeError("Cannot start: already in_progress")
        )
        drive = MagicMock()
        drive.upload_and_share = MagicMock(return_value="https://drive.example/view/1")

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(recording_group_dir="game-x")

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        ttt.fulfill_clip_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_failure_no_fulfill(self, tmp_path):
        """Drive upload failure → no fulfill, request stays in_progress."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()
        drive.upload_and_share = MagicMock(side_effect=RuntimeError("drive boom"))

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(recording_group_dir="game-x")

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        ttt.fulfill_clip_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_external_storage_with_upload_block_uses_resumable_url(
        self, tmp_path
    ):
        """When TTT embeds an upload.resumable_url, soccer-cam PUTs directly to Google."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        # drive_uploader should NOT be used on this path
        drive = MagicMock()
        drive.upload_and_share = MagicMock()

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(
            delivery_method="external_storage",
            recording_group_dir="game-x",
            segments=[
                {"start_time": 0, "end_time": 10, "label": "a"},
                {"start_time": 20, "end_time": 30, "label": "b"},
            ],
        )
        req["upload"] = {
            "provider": "google_drive",
            "resumable_url": "https://uploads.google.com/session-abc",
            "filename": "vs IYSA req12345.mp4",
            "mime_type": "video/mp4",
        }

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "video_grouper.utils.ffmpeg_utils.compile_clips",
                new=AsyncMock(return_value="compiled"),
            ),
            patch(
                "video_grouper.utils.resumable_upload.upload_to_resumable_url",
                new=AsyncMock(return_value="https://drive.google.com/file/d/abc/view"),
            ) as mock_upload,
        ):
            await proc._process_request(req)

        mock_upload.assert_awaited_once()
        # Legacy Drive SDK path must not be used when resumable URL is present
        drive.upload_and_share.assert_not_called()
        ttt.fulfill_clip_request.assert_called_once()
        assert (
            ttt.fulfill_clip_request.call_args.args[1]
            == "https://drive.google.com/file/d/abc/view"
        )

    @pytest.mark.asyncio
    async def test_external_storage_resumable_failure_no_fulfill(self, tmp_path):
        """Resumable upload failure leaves request in_progress (no fulfill call)."""
        from video_grouper.utils.resumable_upload import ResumableUploadError

        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()

        proc = _make_processor(tmp_path, ttt_client=ttt, drive_uploader=drive)
        req = _make_request(
            delivery_method="external_storage", recording_group_dir="game-x"
        )
        req["upload"] = {
            "provider": "google_drive",
            "resumable_url": "https://uploads.google.com/session-x",
            "filename": "x.mp4",
            "mime_type": "video/mp4",
        }

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "video_grouper.utils.resumable_upload.upload_to_resumable_url",
                new=AsyncMock(side_effect=ResumableUploadError("403 forbidden")),
            ),
        ):
            await proc._process_request(req)

        ttt.fulfill_clip_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_delivery_method_no_upload(self, tmp_path):
        """Unknown delivery_method → no upload, no fulfill."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()
        yt = MagicMock()

        proc = _make_processor(
            tmp_path, ttt_client=ttt, drive_uploader=drive, youtube_uploader=yt
        )
        req = _make_request(
            delivery_method="s3",  # not supported
            recording_group_dir="game-x",
        )

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        drive.upload_and_share.assert_not_called()
        yt.upload_video.assert_not_called()
        ttt.fulfill_clip_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_folder_id_no_upload(self, tmp_path):
        """external_storage with no google_drive_folder_id configured → no upload."""
        game_dir = tmp_path / "game-x"
        game_dir.mkdir()
        (game_dir / "combined.mp4").write_bytes(b"\x00")

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        drive = MagicMock()

        proc = _make_processor(
            tmp_path, ttt_client=ttt, drive_uploader=drive, folder_id=None
        )
        req = _make_request(recording_group_dir="game-x")

        with patch(
            "video_grouper.utils.ffmpeg_utils.extract_clip",
            new=AsyncMock(return_value="ok"),
        ):
            await proc._process_request(req)

        drive.upload_and_share.assert_not_called()
        ttt.fulfill_clip_request.assert_not_called()


# ---------------------------------------------------------------------------
# discover_work dedup
# ---------------------------------------------------------------------------


class TestDiscoverWorkDedup:
    @pytest.mark.asyncio
    async def test_duplicate_request_ids_skipped(self, tmp_path):
        """Same request id returned twice → only one processing task created."""
        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ttt.get_pending_clip_requests = MagicMock(
            return_value=[{"id": "dup"}, {"id": "dup"}, {"id": "other"}]
        )

        proc = _make_processor(tmp_path, ttt_client=ttt)

        created = []

        async def fake_process(req):
            created.append(req["id"])

        with patch.object(proc, "_process_request", new=fake_process):
            await proc.discover_work()
            # Let scheduled tasks run
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert sorted(created) == ["dup", "other"]

    @pytest.mark.asyncio
    async def test_not_authenticated_skips_poll(self, tmp_path):
        """TTT auth down → discover_work is a no-op."""
        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=False)
        ttt.get_pending_clip_requests = MagicMock()

        proc = _make_processor(tmp_path, ttt_client=ttt)
        await proc.discover_work()

        ttt.get_pending_clip_requests.assert_not_called()


# ---------------------------------------------------------------------------
# _youtube_metadata
# ---------------------------------------------------------------------------


class TestYoutubeMetadata:
    def test_single_segment_title_uses_label(self, tmp_path):
        proc = _make_processor(tmp_path)
        req = _make_request(
            delivery_method="youtube",
            segments=[{"start_time": 0, "end_time": 10, "label": "goal"}],
        )
        title, desc = proc._youtube_metadata(req)
        assert "IYSA" in title
        assert "goal" in title
        assert "1 clip(s)" in desc

    def test_multi_segment_title_says_highlights(self, tmp_path):
        proc = _make_processor(tmp_path)
        req = _make_request(
            delivery_method="youtube",
            segments=[
                {"start_time": 0, "end_time": 10, "label": "a"},
                {"start_time": 20, "end_time": 30, "label": "b"},
            ],
        )
        title, _ = proc._youtube_metadata(req)
        assert "highlights" in title.lower()

    def test_notes_included_in_description(self, tmp_path):
        proc = _make_processor(tmp_path)
        req = _make_request(
            delivery_method="youtube",
            notes="Focus on jersey #7",
        )
        _, desc = proc._youtube_metadata(req)
        assert "Focus on jersey #7" in desc
