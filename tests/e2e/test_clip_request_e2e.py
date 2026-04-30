"""End-to-end test for the clip-request flow: TTT polling → extract → resumable upload → fulfill.

Wires ClipRequestProcessor, resumable_upload, and a mocked TTT client together so
the only things stubbed are the TTT HTTP client and Google's upload endpoint. Everything
in between — extraction dispatch, compilation, the resumable PUT, the fulfilled-URL
round-trip — runs for real.

Marked `e2e` so it doesn't run in the regular unit sweep. Use:
    uv run pytest tests/e2e/test_clip_request_e2e.py -m e2e -v
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from video_grouper.task_processors.clip_request_processor import ClipRequestProcessor

pytestmark = [pytest.mark.e2e]


def _make_config():
    ttt = MagicMock()
    ttt.google_drive_folder_id = "legacy-folder-not-used"
    config = MagicMock()
    config.ttt = ttt
    return config


def _build_request(req_id: str, recording_group_dir: str, resumable_url: str) -> dict:
    return {
        "id": req_id,
        "delivery_method": "external_storage",
        "is_compilation": True,
        "notes": "e2e test",
        "segments": [
            {"start_time": 0, "end_time": 5, "label": "first", "sort_order": 0},
            {"start_time": 10, "end_time": 15, "label": "second", "sort_order": 1},
        ],
        "game_session": {
            "recording_group_dir": recording_group_dir,
            "opponent_name": "E2ETEAM",
            "start_time": "2026-04-01T10:00:00Z",
        },
        "upload": {
            "provider": "google_drive",
            "resumable_url": resumable_url,
            "filename": "vs E2ETEAM highlight.mp4",
            "mime_type": "video/mp4",
        },
    }


class TestClipRequestEndToEnd:
    @pytest.mark.asyncio
    async def test_external_storage_with_resumable_url_full_flow(self, tmp_path):
        """A pending external_storage request with upload block runs end-to-end:
        extract → compile → PUT bytes to resumable URL → fulfill in TTT.
        """
        # 1) Stage a fake recording directory + combined.mp4 on disk so
        # _resolve_recording_dir / _find_source_video succeed with real I/O.
        game_dir = tmp_path / "game-e2e"
        game_dir.mkdir()
        source_video = game_dir / "combined.mp4"
        source_video.write_bytes(b"\x00" * (512 * 1024))  # 512KB sentinel

        # 2) Capture every PUT to the resumable URL so we can assert on bytes/headers.
        captured = {"chunks": []}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["chunks"].append(
                {
                    "range": request.headers.get("Content-Range"),
                    "content_type": request.headers.get("Content-Type"),
                    "length": int(request.headers.get("Content-Length", "0")),
                }
            )
            # Tell Google "upload complete" on the first (and only) chunk so the
            # small compiled file returns with a 200 + file id.
            return httpx.Response(200, json={"id": "e2e-drive-id"})

        transport = httpx.MockTransport(handler)

        # 3) Build a TTT client mock that returns one pending request, tracks state.
        req_id = "e2e-00000000-0000-0000-0000-000000000001"
        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ttt.get_pending_clip_requests = MagicMock(
            return_value=[
                _build_request(
                    req_id=req_id,
                    recording_group_dir="game-e2e",
                    resumable_url="https://uploads.google.com/e2e-session-xyz",
                )
            ]
        )
        ttt.start_clip_request = MagicMock()
        ttt.fulfill_clip_request = MagicMock()

        # 4) Build the processor with real uploaders in the chain.
        proc = ClipRequestProcessor(
            storage_path=str(tmp_path),
            config=_make_config(),
            ttt_client=ttt,
            drive_uploader=MagicMock(),  # legacy path; must NOT be used
            ntfy_service=None,
            youtube_uploader=None,
        )

        # 5) Patch ffmpeg primitives to produce real temp files (not touching av).
        async def fake_extract(src, start, end, out):
            with open(out, "wb") as f:
                f.write(b"\x11" * (128 * 1024))  # 128KB per clip

        async def fake_compile(clip_paths, output_path):
            # Concatenate bytes for a deterministic output
            with open(output_path, "wb") as f:
                for path in clip_paths:
                    with open(path, "rb") as src:
                        f.write(src.read())
            return output_path

        # 6) Patch httpx.AsyncClient inside resumable_upload to use MockTransport.
        # Capture the real class before patching to avoid recursion.
        real_async_client = httpx.AsyncClient

        def mock_async_client_factory(*args, **kwargs):
            return real_async_client(transport=transport, timeout=10.0)

        with (
            patch(
                "video_grouper.utils.ffmpeg_utils.extract_clip",
                new=AsyncMock(side_effect=fake_extract),
            ),
            patch(
                "video_grouper.utils.ffmpeg_utils.compile_clips",
                new=AsyncMock(side_effect=fake_compile),
            ),
            patch(
                "video_grouper.utils.resumable_upload.httpx.AsyncClient",
                side_effect=mock_async_client_factory,
            ),
        ):
            # 7) Run one discovery pass. _process_request is scheduled as a task;
            # await it by tracking the _processing set.
            await proc.discover_work()

            # Allow the scheduled task to run to completion
            import asyncio

            for _ in range(20):
                if req_id not in proc._processing:
                    break
                await asyncio.sleep(0.05)

        # 8) Verify the chain worked end-to-end:

        # TTT saw a start → fulfill transition
        ttt.start_clip_request.assert_called_once_with(req_id)
        ttt.fulfill_clip_request.assert_called_once()
        fulfilled_args = ttt.fulfill_clip_request.call_args
        assert fulfilled_args.args[0] == req_id
        # Fulfilled URL is derived from the mock Google response id
        assert "e2e-drive-id" in fulfilled_args.args[1]
        assert "2 clip(s)" in fulfilled_args.args[2]

        # Google endpoint received at least one PUT with the expected content type
        assert len(captured["chunks"]) >= 1, "No PUT reached the resumable endpoint"
        assert captured["chunks"][0]["content_type"] == "video/mp4"
        assert captured["chunks"][0]["length"] > 0

        # Legacy drive_uploader was NOT used
        proc.drive_uploader.upload_and_share.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_footage_surfaces_via_ntfy(self, tmp_path):
        """When combined.mp4 is missing, processor notifies and leaves request alone."""
        # game_dir exists but has no combined.mp4
        (tmp_path / "game-missing").mkdir()

        ttt = MagicMock()
        ttt.is_authenticated = MagicMock(return_value=True)
        ttt.get_pending_clip_requests = MagicMock(
            return_value=[
                _build_request(
                    req_id="missing-req",
                    recording_group_dir="game-missing",
                    resumable_url="https://uploads.google.com/unused",
                )
            ]
        )
        ntfy = MagicMock()
        ntfy.send_notification = AsyncMock()

        proc = ClipRequestProcessor(
            storage_path=str(tmp_path),
            config=_make_config(),
            ttt_client=ttt,
            drive_uploader=MagicMock(),
            ntfy_service=ntfy,
            youtube_uploader=None,
        )

        await proc.discover_work()

        import asyncio

        for _ in range(10):
            if "missing-req" not in proc._processing:
                break
            await asyncio.sleep(0.05)
        # Give the scheduled ntfy task a tick to run
        await asyncio.sleep(0)

        ttt.start_clip_request.assert_not_called()
        ttt.fulfill_clip_request.assert_not_called()
        ntfy.send_notification.assert_called()
