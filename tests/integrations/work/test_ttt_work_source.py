"""TTTWorkSource on its own: every verb x kind maps onto the right TTT client
call with the right arguments; listing isolates kinds and is silent while
signed out; every other verb lets client errors through, as the processors
saw them before the port."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.api_integrations.ttt_api import TTTApiClient, TTTApiError
from video_grouper.integrations.registry import IntegrationContext
from video_grouper.integrations.work import (
    CLIP,
    HIGHLIGHT_REEL,
    KINDS,
    PROCESS_RECORDING,
    REPROCESS,
    WorkItem,
)
from video_grouper.integrations.work.sources.ttt import TTTWorkSource

_LISTS = {
    CLIP: "get_pending_clip_requests",
    HIGHLIGHT_REEL: "get_pending_highlights",
    PROCESS_RECORDING: "get_pending_jobs",
    REPROCESS: "get_reprocess_queue",
}


def _client(signed_in=True):
    c = MagicMock()
    c.is_authenticated.return_value = signed_in
    for method in _LISTS.values():
        getattr(c, method).return_value = []
    return c


def _source(client=None, camera_id="cam-xyz"):
    client = client or _client()
    return TTTWorkSource(client, SimpleNamespace(camera_id=camera_id)), client


def _item(kind, payload=None, status="pending", item_id="w-1"):
    return WorkItem(kind, item_id, {"id": item_id, **(payload or {})}, status)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_chosen_only_when_ttt_enabled():
    off = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert TTTWorkSource.from_context(off) is None

    client = _client()
    on = IntegrationContext(
        SimpleNamespace(ttt=SimpleNamespace(enabled=True, camera_id="c")),
        ".",
        {"ttt": client},
    )
    source = TTTWorkSource.from_context(on)
    assert isinstance(source, TTTWorkSource)
    assert source._client is client  # the one shared session


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_lists_every_kind_as_work_items():
    source, client = _source()
    client.get_pending_clip_requests.return_value = [{"id": "c1"}, {"no": "id"}]
    client.get_pending_highlights.return_value = [{"id": "r1", "source": "manual"}]
    client.get_pending_jobs.return_value = [{"id": "j1"}]
    client.get_reprocess_queue.return_value = [
        {"id": "p1", "status": "pending"},
        {"id": "p2", "status": "running", "cancel_requested": True},
        {"id": "p3"},
    ]

    items = await source.pending()

    assert [(i.kind, i.id, i.status) for i in items] == [
        (CLIP, "c1", "pending"),
        (HIGHLIGHT_REEL, "r1", "pending"),
        (PROCESS_RECORDING, "j1", "pending"),
        (REPROCESS, "p1", "pending"),
        (REPROCESS, "p2", "running"),
        (REPROCESS, "p3", ""),
    ]
    assert items[1].payload == {"id": "r1", "source": "manual"}
    assert items[4].payload["cancel_requested"] is True


@pytest.mark.asyncio
async def test_pending_asks_only_for_the_requested_kinds():
    source, client = _source()
    await source.pending([CLIP, REPROCESS])
    client.get_pending_clip_requests.assert_called_once_with()
    client.get_reprocess_queue.assert_called_once_with()
    client.get_pending_highlights.assert_not_called()
    client.get_pending_jobs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_id, sent", [("cam-xyz", "cam-xyz"), ("", None)])
async def test_pending_reels_are_scoped_to_this_camera(camera_id, sent):
    source, client = _source(camera_id=camera_id)
    await source.pending([HIGHLIGHT_REEL])
    client.get_pending_highlights.assert_called_once_with(sent)


@pytest.mark.asyncio
async def test_nothing_is_listed_while_signed_out():
    source, client = _source(_client(signed_in=False))
    assert await source.pending() == []
    for method in _LISTS.values():
        getattr(client, method).assert_not_called()


@pytest.mark.asyncio
async def test_pending_treats_an_auth_check_error_as_signed_out():
    client = _client()
    client.is_authenticated.side_effect = RuntimeError("token file unreadable")
    source, _ = _source(client)
    assert await source.pending() == []
    client.get_pending_clip_requests.assert_not_called()


@pytest.mark.asyncio
async def test_one_failing_kind_does_not_hide_the_others(caplog):
    source, client = _source()
    client.get_pending_clip_requests.side_effect = RuntimeError("kaboom")
    client.get_pending_highlights.return_value = [{"id": "r1"}]
    client.get_pending_jobs.return_value = None  # 204

    with caplog.at_level(logging.ERROR):
        items = await source.pending()

    assert [(i.kind, i.id) for i in items] == [(HIGHLIGHT_REEL, "r1")]
    assert any("clip" in r.message and r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_reprocess_queue_failure_is_quiet(caplog):
    # 403 = "not a camera-manager for any team": logged at debug only.
    source, client = _source()
    client.get_reprocess_queue.side_effect = TTTApiError("403", status_code=403)
    with caplog.at_level(logging.DEBUG):
        assert await source.pending([REPROCESS]) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("reprocess: queue poll failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pending_lets_cancellation_through():
    source, client = _source()
    client.get_pending_clip_requests.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await source.pending([CLIP])


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_clip_starts_the_request():
    source, client = _source()
    client.start_clip_request.return_value = None
    assert await source.claim(_item(CLIP)) == {}
    client.start_clip_request.assert_called_once_with("w-1")


@pytest.mark.asyncio
async def test_claim_clip_already_started_is_ours_to_continue():
    source, client = _source()
    client.start_clip_request.side_effect = TTTApiError(
        "Cannot start: already in_progress", status_code=409
    )
    assert await source.claim(_item(CLIP)) == {}


@pytest.mark.asyncio
async def test_claim_clip_other_errors_propagate():
    source, client = _source()
    client.start_clip_request.side_effect = TTTApiError("boom", status_code=500)
    with pytest.raises(TTTApiError):
        await source.claim(_item(CLIP))


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_id, sent", [("cam-xyz", "cam-xyz"), (None, "")])
async def test_claim_reel_sends_camera_id_and_passes_a_lost_race_through(
    camera_id, sent
):
    source, client = _source(camera_id=camera_id)
    client.claim_highlight.return_value = None  # 409
    assert await source.claim(_item(HIGHLIGHT_REEL)) is None
    client.claim_highlight.assert_called_once_with("w-1", sent)


@pytest.mark.asyncio
async def test_claim_job_and_reprocess():
    source, client = _source()
    client.claim_job.return_value = None
    client.claim_reprocess_request.return_value = {"id": "w-1", "kind": "restart"}

    assert await source.claim(_item(PROCESS_RECORDING)) == {}
    assert await source.claim(_item(REPROCESS)) == {"id": "w-1", "kind": "restart"}
    client.claim_job.assert_called_once_with("w-1")
    client.claim_reprocess_request.assert_called_once_with("w-1")


# ---------------------------------------------------------------------------
# details / latest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, called, not_called",
    [
        ({}, "get_highlight_game_clips", "get_highlight_moment_clips"),
        (
            {"source": "manual"},
            "get_highlight_game_clips",
            "get_highlight_moment_clips",
        ),
        (
            {"source": "moment_tagger"},
            "get_highlight_moment_clips",
            "get_highlight_game_clips",
        ),
    ],
)
async def test_reel_details_fetch_the_clips_for_the_reel_source(
    payload, called, not_called
):
    source, client = _source()
    getattr(client, called).return_value = [{"id": "clip-0"}]
    details = await source.details(_item(HIGHLIGHT_REEL, payload))
    assert details == {"clips": [{"id": "clip-0"}]}
    getattr(client, called).assert_called_once_with("w-1")
    getattr(client, not_called).assert_not_called()


@pytest.mark.asyncio
async def test_reprocess_details_fetch_the_recording():
    source, client = _source()
    client.get_camera_recording.return_value = {"id": "rec-1", "file_group": "g"}
    details = await source.details(_item(REPROCESS, {"recording_id": "rec-1"}))
    assert details == {"recording": {"id": "rec-1", "file_group": "g"}}
    client.get_camera_recording.assert_called_once_with("rec-1")


@pytest.mark.asyncio
async def test_reel_latest_refetches_the_reel():
    source, client = _source()
    client.get_highlight.return_value = {"id": "w-1", "youtube_video_id": "YT"}
    assert await source.latest(_item(HIGHLIGHT_REEL)) == {
        "id": "w-1",
        "youtube_video_id": "YT",
    }
    client.get_highlight.assert_called_once_with("w-1")


# ---------------------------------------------------------------------------
# progress / blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_per_kind():
    source, client = _source()

    await source.progress(_item(HIGHLIGHT_REEL), "trimming", 33)
    client.update_highlight_progress.assert_called_once_with(
        "w-1", stage="trimming", percent=33
    )

    await source.progress(
        _item(PROCESS_RECORDING), "downloading", 0, {"message": "Starting download"}
    )
    client.update_job_progress.assert_called_once_with(
        "w-1", "downloading", {"percent": 0, "message": "Starting download"}
    )

    await source.progress(_item(REPROCESS, status="claimed"), "stabilize")
    client.update_reprocess_status.assert_called_once_with(
        "w-1", "running", "stabilize", None
    )


@pytest.mark.asyncio
async def test_blocked_reel_reports_this_camera():
    source, client = _source()
    await source.blocked(_item(HIGHLIGHT_REEL), "Clip 1 source dir not found")
    client.report_blocker.assert_called_once_with(
        "w-1", "cam-xyz", "Clip 1 source dir not found"
    )


# ---------------------------------------------------------------------------
# complete / fail / cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_per_kind():
    source, client = _source()

    await source.complete(_item(CLIP), {"url": "https://u", "notes": "2 clip(s)"})
    client.fulfill_clip_request.assert_called_once_with("w-1", "https://u", "2 clip(s)")

    await source.complete(
        _item(HIGHLIGHT_REEL), {"file_path": "/h/r.mp4", "youtube_video_id": "YT"}
    )
    client.complete_highlight.assert_called_once_with(
        "w-1", file_path="/h/r.mp4", youtube_video_id="YT"
    )

    await source.complete(_item(PROCESS_RECORDING), {"youtube_url": "https://y"})
    client.complete_job.assert_called_once_with("w-1", {"youtube_url": "https://y"})

    await source.complete(_item(REPROCESS), {})
    client.update_reprocess_status.assert_called_once_with(
        "w-1", "completed", None, None
    )


@pytest.mark.asyncio
async def test_fail_per_kind():
    source, client = _source()

    await source.fail(_item(HIGHLIGHT_REEL), "x" * 600)
    client.fail_highlight.assert_called_once_with("w-1", "x" * 500)

    await source.fail(_item(PROCESS_RECORDING), "Combined video not found")
    client.fail_job.assert_called_once_with("w-1", "Combined video not found")

    await source.fail(_item(REPROCESS), "no local dir")
    client.update_reprocess_status.assert_called_once_with(
        "w-1", "failed", None, "no local dir"
    )


@pytest.mark.asyncio
async def test_reprocess_failed_without_a_message_sends_none():
    source, client = _source()
    await source.fail(_item(REPROCESS), None)
    client.update_reprocess_status.assert_called_once_with("w-1", "failed", None, None)


@pytest.mark.asyncio
async def test_cancelled_reprocess():
    source, client = _source()
    await source.cancelled(_item(REPROCESS, status="running"))
    client.update_reprocess_status.assert_called_once_with(
        "w-1", "cancelled", None, None
    )


# ---------------------------------------------------------------------------
# What TTT can't express, and errors
# ---------------------------------------------------------------------------

_UNSUPPORTED = [
    ("details", CLIP, ()),
    ("details", PROCESS_RECORDING, ()),
    ("latest", CLIP, ()),
    ("latest", PROCESS_RECORDING, ()),
    ("latest", REPROCESS, ()),
    ("progress", CLIP, ("step",)),
    ("blocked", CLIP, ("reason",)),
    ("blocked", PROCESS_RECORDING, ("reason",)),
    ("blocked", REPROCESS, ("reason",)),
    ("fail", CLIP, ("error",)),
    ("cancelled", CLIP, ()),
    ("cancelled", HIGHLIGHT_REEL, ()),
    ("cancelled", PROCESS_RECORDING, ()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("verb, kind, args", _UNSUPPORTED)
async def test_calls_ttt_cannot_express_raise_and_call_nothing(verb, kind, args):
    client = MagicMock(spec=TTTApiClient)
    source = TTTWorkSource(client, SimpleNamespace(camera_id="c"))
    with pytest.raises(NotImplementedError, match=verb):
        await getattr(source, verb)(_item(kind), *args)
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_unknown_kind_raises():
    source, _ = _source()
    with pytest.raises(NotImplementedError):
        await source.claim(_item("teleport"))
    assert set(KINDS) == {CLIP, HIGHLIGHT_REEL, PROCESS_RECORDING, REPROCESS}


@pytest.mark.asyncio
async def test_client_errors_propagate_from_reports():
    source, client = _source()
    client.complete_job.side_effect = TTTApiError("502", status_code=502)
    with pytest.raises(TTTApiError):
        await source.complete(_item(PROCESS_RECORDING), {})


@pytest.mark.asyncio
async def test_signed_out_reports_raise_without_any_request(tmp_path):
    """The real client refuses before touching the network when there is no
    session, so a report while signed out reaches nothing."""
    client = TTTApiClient(
        supabase_url="https://sb.example",
        anon_key="anon",
        api_base_url="https://ttt.example",
        storage_path=str(tmp_path),
    )
    client._http = MagicMock()
    source = TTTWorkSource(client, SimpleNamespace(camera_id="c"))

    assert await source.pending() == []
    with pytest.raises(TTTApiError, match="Not authenticated"):
        await source.claim(_item(REPROCESS))
    with pytest.raises(TTTApiError, match="Not authenticated"):
        await source.complete(_item(CLIP), {"url": "u"})
    client._http.request.assert_not_called()
