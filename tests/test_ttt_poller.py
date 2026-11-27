"""Tests for TTTPoller — the single polling entrypoint that asks the work
source (IWorkSource) for new work and enqueues it onto each kind's
QueueProcessor. Tested against the interface (FakeWorkSource); the TTT
mapping is tested in tests/integrations/work/test_ttt_work_source.py."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from video_grouper.integrations.work import (
    CLIP,
    HIGHLIGHT_REEL,
    PROCESS_RECORDING,
    REPROCESS,
    WorkItem,
)
from video_grouper.integrations.work.fake import FakeWorkSource
from video_grouper.task_processors.tasks.clip.clip_request_task import (
    ClipRequestTask,
)
from video_grouper.task_processors.tasks.ttt.highlight_reel_task import (
    HighlightReelTask,
)
from video_grouper.task_processors.tasks.ttt.reprocess_request_task import (
    ReprocessRequestTask,
)
from video_grouper.task_processors.tasks.ttt.ttt_job_task import TTTJobTask
from video_grouper.task_processors.ttt_poller import TTTPoller


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _mk_config(*, ttt_enabled: bool = True, camera_id: str | None = None):
    cfg = MagicMock()
    cfg.ttt.enabled = ttt_enabled
    cfg.ttt.camera_id = camera_id
    cfg.ttt.machine_name = "test"
    cfg.pipeline.is_active.return_value = False
    cfg.camera.type = "dahua"
    cfg.camera.device_ip = "127.0.0.1"
    return cfg


def _mk_ttt(authenticated: bool = True):
    c = MagicMock()
    c.is_authenticated.return_value = authenticated
    return c


def _mk_processor():
    p = MagicMock()
    p.add_work = AsyncMock()
    return p


def _mk_poller(tmp_path, work, ttt=None, *, ttt_enabled=True, **procs):
    return TTTPoller(
        storage_path=str(tmp_path),
        config=_mk_config(ttt_enabled=ttt_enabled),
        ttt_client=ttt if ttt is not None else _mk_ttt(),
        work=work,
        **procs,
    )


def _enqueued(processor):
    return [call.args[0] for call in processor.add_work.await_args_list]


# ---------------------------------------------------------------------------
# One pending() per tick, dispatched by kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_kind_is_enqueued_on_its_processor(tmp_path):
    work = FakeWorkSource(
        [
            WorkItem(CLIP, "c1", {"id": "c1", "segments": []}),
            WorkItem(CLIP, "c2", {"id": "c2"}),
            WorkItem(HIGHLIGHT_REEL, "r1", {"id": "r1"}),
            WorkItem(PROCESS_RECORDING, "j1", {"id": "j1"}),
            WorkItem(REPROCESS, "rp1", {"id": "rp1"}),
        ]
    )
    procs = {
        "clip_request_processor": _mk_processor(),
        "highlight_reel_processor": _mk_processor(),
        "ttt_job_processor": _mk_processor(),
        "reprocess_request_processor": _mk_processor(),
    }
    poller = _mk_poller(tmp_path, work, **procs)

    await poller.discover_work()

    assert len(work.pending_kinds) == 1  # one pending() per tick
    clips = _enqueued(procs["clip_request_processor"])
    assert all(isinstance(t, ClipRequestTask) for t in clips)
    assert [t.ttt_id for t in clips] == ["c1", "c2"]
    assert clips[0].payload == {"id": "c1", "segments": []}
    (reel,) = _enqueued(procs["highlight_reel_processor"])
    assert isinstance(reel, HighlightReelTask) and reel.ttt_id == "r1"
    (job,) = _enqueued(procs["ttt_job_processor"])
    assert isinstance(job, TTTJobTask) and job.ttt_id == "j1"
    (rp,) = _enqueued(procs["reprocess_request_processor"])
    assert isinstance(rp, ReprocessRequestTask) and rp.ttt_id == "rp1"


@pytest.mark.asyncio
async def test_only_wired_kinds_are_asked_for(tmp_path):
    work = FakeWorkSource()
    poller = _mk_poller(
        tmp_path,
        work,
        clip_request_processor=_mk_processor(),
        reprocess_request_processor=_mk_processor(),
    )
    await poller.discover_work()
    assert [sorted(k) for k in work.pending_kinds] == [sorted([CLIP, REPROCESS])]


@pytest.mark.asyncio
async def test_no_wired_kinds_means_no_poll(tmp_path):
    work = FakeWorkSource()
    poller = _mk_poller(tmp_path, work)
    await poller.discover_work()
    assert work.pending_kinds == []


@pytest.mark.asyncio
async def test_null_work_source_by_default(tmp_path):
    p_clip = _mk_processor()
    poller = TTTPoller(
        storage_path=str(tmp_path),
        config=_mk_config(),
        ttt_client=_mk_ttt(),
        clip_request_processor=p_clip,
    )
    await poller.discover_work()
    p_clip.add_work.assert_not_called()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_kind_raising_does_not_starve_others(tmp_path, caplog):
    work = FakeWorkSource(
        [WorkItem(CLIP, "c1", {"id": "c1"}), WorkItem(HIGHLIGHT_REEL, "r1", {})]
    )
    p_clip = _mk_processor()
    p_clip.add_work.side_effect = RuntimeError("kaboom")
    p_hl = _mk_processor()
    poller = _mk_poller(
        tmp_path, work, clip_request_processor=p_clip, highlight_reel_processor=p_hl
    )

    await poller.discover_work()

    assert any("clip" in rec.message and "c1" in rec.message for rec in caplog.records)
    p_hl.add_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_listing_still_refreshes_the_schedule(tmp_path, caplog):
    work = FakeWorkSource(errors={"pending": RuntimeError("down")})
    schedule = MagicMock()
    poller = _mk_poller(
        tmp_path,
        work,
        clip_request_processor=_mk_processor(),
        schedule_service=schedule,
    )
    await poller.discover_work()
    assert any("work poll raised" in rec.message for rec in caplog.records)
    schedule.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_error_propagates(tmp_path):
    work = FakeWorkSource(errors={"pending": asyncio.CancelledError()})
    poller = _mk_poller(tmp_path, work, clip_request_processor=_mk_processor())
    with pytest.raises(asyncio.CancelledError):
        await poller.discover_work()


# ---------------------------------------------------------------------------
# The schedule refresh keeps the TTT gate; device-link calls are not here
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_feature_makes_no_device_link_calls(tmp_path):
    # Announcing the install + its heartbeat moved to DeviceLinkProcessor
    # (tests/test_device_link_processor.py).
    ttt = _mk_ttt()
    work = FakeWorkSource([WorkItem(PROCESS_RECORDING, "j1", {"id": "j1"})])
    p_job = _mk_processor()
    poller = _mk_poller(tmp_path, work, ttt, ttt_job_processor=p_job)

    await poller.discover_work()

    ttt.register_service.assert_not_called()
    ttt.send_heartbeat.assert_not_called()
    p_job.add_work.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, authenticated", [(False, True), (True, False)])
async def test_ttt_calls_skipped_without_a_ttt_session(
    tmp_path, enabled, authenticated
):
    ttt = _mk_ttt(authenticated=authenticated)
    schedule = MagicMock()
    poller = _mk_poller(
        tmp_path,
        FakeWorkSource(),
        ttt,
        ttt_enabled=enabled,
        ttt_job_processor=_mk_processor(),
        schedule_service=schedule,
    )
    await poller.discover_work()
    schedule.refresh.assert_not_called()


@pytest.mark.asyncio
async def test_poll_schedule_calls_refresh_when_set(tmp_path):
    schedule_service = MagicMock()
    schedule_service.refresh.return_value = True
    poller = TTTPoller(
        storage_path=str(tmp_path),
        config=_mk_config(),
        ttt_client=_mk_ttt(),
        schedule_service=schedule_service,
    )
    await poller._poll_schedule()
    schedule_service.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_poll_schedule_noop_when_none(tmp_path):
    poller = TTTPoller(
        storage_path=str(tmp_path),
        config=_mk_config(),
        ttt_client=_mk_ttt(),
    )
    # Should not raise and should be a no-op
    await poller._poll_schedule()


# ---------------------------------------------------------------------------
# Reprocess in-flight: cancel + progress reporting
# ---------------------------------------------------------------------------

_GROUP = "2026.06.06-15.01.33"


def _seed_reprocess(storage, state_status: str, running_step: str | None = None):
    g = storage / _GROUP
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": state_status}))
    if running_step:
        (g / "pipeline_state.json").write_text(
            json.dumps({"steps": [{"step_id": running_step, "status": "running"}]})
        )
    return g


def _in_flight(status="running", **payload):
    return WorkItem(
        REPROCESS,
        "rp1",
        {"id": "rp1", "status": status, "recording_id": "rec-1", **payload},
        status,
    )


def _reprocess_work(item, **kw):
    return FakeWorkSource(
        [item],
        details={"rp1": {"recording": {"id": "rec-1", "file_group": _GROUP}}},
        **kw,
    )


@pytest.mark.asyncio
async def test_reprocess_cancel_writes_marker(tmp_path):
    group = _seed_reprocess(tmp_path, "running")
    work = _reprocess_work(_in_flight(cancel_requested=True))
    p_rp = _mk_processor()
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=p_rp)

    await poller.discover_work()

    assert (group / "cancel_request.json").exists()
    p_rp.add_work.assert_not_called()


@pytest.mark.asyncio
async def test_reprocess_progress_reports_the_running_step(tmp_path):
    _seed_reprocess(tmp_path, "processing", running_step="stabilize")
    work = _reprocess_work(_in_flight("claimed", cancel_requested=False))
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=_mk_processor())

    await poller.discover_work()

    (call,) = work.called("progress")
    assert (call.kind, call.id, call.args[0]) == (REPROCESS, "rp1", "stabilize")
    assert poller._reprocess_tracked["rp1"] == tmp_path / _GROUP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local, verb, args",
    [
        ("pipeline_complete", "complete", ({},)),
        ("pipeline_cancelled", "cancelled", ()),
        ("pipeline_failed", "fail", ("render blew up",)),
    ],
)
async def test_reprocess_terminal_local_state_is_reported_and_untracked(
    tmp_path, local, verb, args
):
    group = _seed_reprocess(tmp_path, local)
    (group / "pipeline_state.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"step_id": "render", "status": "failed", "error": "render blew up"}
                ]
            }
        )
        if verb == "fail"
        else "{}"
    )
    work = _reprocess_work(_in_flight())
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=_mk_processor())

    await poller.discover_work()

    (call,) = work.called(verb)
    assert call.id == "rp1" and call.args == args
    assert "rp1" not in poller._reprocess_tracked


@pytest.mark.asyncio
async def test_reprocess_unchanged_status_is_not_reported(tmp_path):
    _seed_reprocess(tmp_path, "processing", running_step="stabilize")
    work = _reprocess_work(_in_flight("running"))
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=_mk_processor())

    await poller.discover_work()

    assert work.called("progress") == []


@pytest.mark.asyncio
async def test_reprocess_report_failure_keeps_tracking(tmp_path):
    _seed_reprocess(tmp_path, "pipeline_complete")
    work = _reprocess_work(_in_flight(), errors={"complete": RuntimeError("502")})
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=_mk_processor())

    await poller.discover_work()

    assert poller._reprocess_tracked["rp1"] == tmp_path / _GROUP


@pytest.mark.asyncio
async def test_reprocess_unresolvable_recording_does_nothing(tmp_path):
    # No local group dir for the recording's file_group.
    work = _reprocess_work(_in_flight(cancel_requested=True))
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=_mk_processor())

    await poller.discover_work()

    assert [c.verb for c in work.calls] == ["details", "details"]
    assert poller._reprocess_tracked == {}


@pytest.mark.asyncio
async def test_reprocess_other_statuses_are_ignored(tmp_path):
    work = FakeWorkSource([_in_flight("completed"), _in_flight("")])
    p_rp = _mk_processor()
    poller = _mk_poller(tmp_path, work, reprocess_request_processor=p_rp)

    await poller.discover_work()

    p_rp.add_work.assert_not_called()
    assert work.calls == []
