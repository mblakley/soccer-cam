"""Unit tests for phase-detection game start (decisions 1, 7, 8).

Two layers:
  * the resolver ``maybe_resolve_phase_game_start`` — gating + offset write +
    fallback (detector mocked, real filesystem temp dirs);
  * the NtfyProcessor wiring — a handled fit skips the NTFY game-start walk; an
    unhandled one (Dahua / ntfy mode / rejected) falls back to it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import video_grouper.task_processors.phase_game_start as pgs
from video_grouper.models import MatchInfo


def _cfg(method: str = "phase_detection", cam_type: str = "reolink"):
    return SimpleNamespace(
        processing=SimpleNamespace(game_start_method=method),
        cameras=[SimpleNamespace(type=cam_type)],
    )


def _make_group(tmp_path):
    group = tmp_path / "2026.06.18-10.00.00"
    group.mkdir()
    combined = group / "combined.mp4"
    combined.write_bytes(b"x")  # must exist; the detector itself is mocked
    return str(group), str(combined)


def _async_return(value):
    async def _fake(*_a, **_k):
        return value

    return _fake


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_enabled_for_reolink_phase_detection():
    assert pgs.phase_game_start_enabled(_cfg("phase_detection", "reolink")) is True


def test_disabled_for_dahua():
    assert pgs.phase_game_start_enabled(_cfg("phase_detection", "dahua")) is False


def test_disabled_for_ntfy_mode():
    assert pgs.phase_game_start_enabled(_cfg("ntfy", "reolink")) is False


def test_method_defaults_to_phase_detection_when_unset():
    cfg = SimpleNamespace(
        processing=SimpleNamespace(), cameras=[SimpleNamespace(type="reolink")]
    )
    assert pgs.phase_game_start_enabled(cfg) is True


def test_camera_type_via_section_accessor_fallback():
    # configparser-style config: no `cameras` list, a `camera.type` accessor.
    cfg = SimpleNamespace(
        processing=SimpleNamespace(game_start_method="phase_detection"),
        cameras=None,
        camera=SimpleNamespace(type="reolink"),
    )
    assert pgs.phase_game_start_enabled(cfg) is True


# --------------------------------------------------------------------------
# Resolver: Reolink + ok=True -> sets offset from KO + backup, persists phases
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reolink_ok_sets_offset_and_persists_phases(tmp_path, monkeypatch):
    group, combined = _make_group(tmp_path)
    fake = {
        "ok": True,
        "ko_trustworthy": True,
        "times": {
            "kickoff": 600.0,
            "halftime": 1500.0,
            "second_half": 2100.0,
            "end": 3600.0,
        },
        "reasons": [],
        "used": "whistle+kick",
    }
    monkeypatch.setattr(pgs, "_run_detector", _async_return(fake))

    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), storage_path=str(tmp_path)
    )
    assert handled is True

    # 600s kickoff - 60s phase backup = 540s -> "09:00".
    assert pgs.PHASE_KO_TRIM_BACKUP_SECONDS == 60
    mi = MatchInfo.get_or_create(group, str(tmp_path))[0]
    assert mi.start_time_offset == "09:00"

    # Phases persisted to the group state (source phase_fused) for S2.
    from video_grouper.models import DirectoryState

    stored = DirectoryState(group, str(tmp_path)).get_game_phases()
    assert stored is not None
    assert stored["source"] == "phase_fused"
    assert stored["ok"] is True
    assert stored["times"]["end"] == 3600.0


@pytest.mark.asyncio
async def test_reolink_kickoff_clamped_at_zero(tmp_path, monkeypatch):
    """A kickoff smaller than the backup floors start at 00:00 (never negative)."""
    group, combined = _make_group(tmp_path)
    monkeypatch.setattr(
        pgs,
        "_run_detector",
        _async_return({"ok": True, "ko_trustworthy": True, "times": {"kickoff": 30.0}}),
    )
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), str(tmp_path)
    )
    assert handled is True
    mi = MatchInfo.get_or_create(group, str(tmp_path))[0]
    assert mi.start_time_offset == "00:00"


@pytest.mark.asyncio
async def test_ko_trustworthy_but_not_ok_still_trims(tmp_path, monkeypatch):
    """The gate is ``ko_trustworthy``, not ``ok``: a game with an exact
    kickoff-whistle KO but a failed full-fit (ok=False) is still auto-trimmed
    (05.27 / 06.06-Sullivan: KO -1s, ok=False, localized -> trusted)."""
    group, combined = _make_group(tmp_path)
    monkeypatch.setattr(
        pgs,
        "_run_detector",
        _async_return(
            {
                "ok": False,
                "ko_trustworthy": True,
                "reasons": ["asym=16.3"],
                "times": {"kickoff": 600.0},
            }
        ),
    )
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), str(tmp_path)
    )
    assert handled is True
    mi = MatchInfo.get_or_create(group, str(tmp_path))[0]
    assert mi.start_time_offset == "09:00"  # 600s - 60s backup


# --------------------------------------------------------------------------
# Resolver: fallbacks (return False, nothing written)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_untrustworthy_ko_falls_back(tmp_path, monkeypatch):
    """A KO that is not ``ko_trustworthy`` (symmetric prior / non-localized
    warm-up whistle: 03.21, 05.30) falls back to the NTFY walk — even if a
    kickoff time was produced."""
    group, combined = _make_group(tmp_path)
    monkeypatch.setattr(
        pgs,
        "_run_detector",
        _async_return(
            {
                "ok": False,
                "ko_trustworthy": False,
                "times": {"kickoff": 600.0},
                "reasons": ["asym"],
            }
        ),
    )
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), str(tmp_path)
    )
    assert handled is False
    mi = MatchInfo.get_or_create(group, str(tmp_path))[0]
    assert not (mi.start_time_offset or "").strip()  # unchanged


@pytest.mark.asyncio
async def test_reolink_none_result_falls_back(tmp_path, monkeypatch):
    group, combined = _make_group(tmp_path)
    monkeypatch.setattr(pgs, "_run_detector", _async_return(None))
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), str(tmp_path)
    )
    assert handled is False


@pytest.mark.asyncio
async def test_dahua_does_not_run_detector(tmp_path, monkeypatch):
    group, combined = _make_group(tmp_path)
    calls = {"n": 0}

    async def fake_run(*_a, **_k):
        calls["n"] += 1
        return {"ok": True, "times": {"kickoff": 0.0}}

    monkeypatch.setattr(pgs, "_run_detector", fake_run)
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(cam_type="dahua"), str(tmp_path)
    )
    assert handled is False
    assert calls["n"] == 0  # gated out before any detection work


@pytest.mark.asyncio
async def test_ntfy_mode_does_not_run_detector(tmp_path, monkeypatch):
    group, combined = _make_group(tmp_path)
    calls = {"n": 0}

    async def fake_run(*_a, **_k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(pgs, "_run_detector", fake_run)
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(method="ntfy"), str(tmp_path)
    )
    assert handled is False
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_already_set_returns_handled_without_rerunning(tmp_path, monkeypatch):
    """An already-resolved start is handled without re-running the detector."""
    group, combined = _make_group(tmp_path)
    MatchInfo.update_game_times(
        group, start_time_offset="05:00", storage_path=str(tmp_path)
    )
    calls = {"n": 0}

    async def fake_run(*_a, **_k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(pgs, "_run_detector", fake_run)
    handled = await pgs.maybe_resolve_phase_game_start(
        group, combined, _cfg(), str(tmp_path)
    )
    assert handled is True
    assert calls["n"] == 0


# --------------------------------------------------------------------------
# NtfyProcessor wiring: handled -> skip the walk; unhandled -> run it
# --------------------------------------------------------------------------


def _build_processor(mock_config, tmp_path, *, video_processor=None):
    from video_grouper.task_processors.ntfy_processor import NtfyProcessor
    from video_grouper.utils.config import ProcessingConfig

    mock_config.processing = ProcessingConfig()  # game_start_method defaults to phase

    ntfy_service = MagicMock()
    ntfy_service.has_been_processed.return_value = False
    ntfy_service.is_waiting_for_input.return_value = False
    ntfy_service.is_failed_to_send.return_value = False

    processor = NtfyProcessor(
        storage_path=str(tmp_path),
        config=mock_config,
        ntfy_service=ntfy_service,
        match_info_service=MagicMock(),
    )
    processor._queue = asyncio.Queue()
    processor.video_processor = video_processor
    return processor


def _drain_task_types(processor) -> list[str]:
    types = []
    while not processor._queue.empty():
        _, _, task = processor._queue.get_nowait()
        types.append(task.__class__.__name__)
    return types


@pytest.mark.asyncio
async def test_wiring_handled_skips_game_start_walk(mock_config, tmp_path, monkeypatch):
    """maybe_resolve True -> the NTFY GameStartTask is NOT enqueued."""
    processor = _build_processor(mock_config, tmp_path)
    monkeypatch.setattr(pgs, "maybe_resolve_phase_game_start", _async_return(True))

    mock_match_info = MagicMock()
    mock_match_info.is_populated.return_value = False
    mock_match_info.get_team_info.return_value = {
        "my_team_name": "A",
        "opponent_team_name": "B",
        "location": "Home",
    }
    mock_match_info.start_time_offset = "06:00"

    with patch("video_grouper.task_processors.ntfy_processor.MatchInfo") as mock_mi:
        mock_mi.get_or_create.return_value = (mock_match_info, False)
        await processor.request_match_info_for_directory(
            str(tmp_path / "g"), str(tmp_path / "g" / "combined.mp4")
        )

    assert "GameStartTask" not in _drain_task_types(processor)


@pytest.mark.asyncio
async def test_wiring_unhandled_runs_game_start_walk(
    mock_config, tmp_path, monkeypatch
):
    """maybe_resolve False (e.g. rejected fit) -> GameStartTask IS enqueued."""
    processor = _build_processor(mock_config, tmp_path)
    monkeypatch.setattr(pgs, "maybe_resolve_phase_game_start", _async_return(False))

    mock_match_info = MagicMock()
    mock_match_info.is_populated.return_value = False
    mock_match_info.get_team_info.return_value = {
        "my_team_name": "A",
        "opponent_team_name": "B",
        "location": "Home",
    }
    mock_match_info.start_time_offset = ""

    with patch("video_grouper.task_processors.ntfy_processor.MatchInfo") as mock_mi:
        mock_mi.get_or_create.return_value = (mock_match_info, False)
        await processor.request_match_info_for_directory(
            str(tmp_path / "g"), str(tmp_path / "g" / "combined.mp4")
        )

    assert "GameStartTask" in _drain_task_types(processor)


@pytest.mark.asyncio
async def test_wiring_dahua_falls_back_to_walk(mock_config, tmp_path):
    """Real resolver + a Dahua camera -> gated out, GameStartTask enqueued."""
    processor = _build_processor(mock_config, tmp_path)
    mock_config.set("CAMERA", "type", "dahua")  # gate phase detection out

    mock_match_info = MagicMock()
    mock_match_info.is_populated.return_value = False
    mock_match_info.get_team_info.return_value = {
        "my_team_name": "A",
        "opponent_team_name": "B",
        "location": "Home",
    }
    mock_match_info.start_time_offset = ""

    with patch("video_grouper.task_processors.ntfy_processor.MatchInfo") as mock_mi:
        mock_mi.get_or_create.return_value = (mock_match_info, False)
        await processor.request_match_info_for_directory(
            str(tmp_path / "g"), str(tmp_path / "g" / "combined.mp4")
        )

    assert "GameStartTask" in _drain_task_types(processor)


# --------------------------------------------------------------------------
# GameStartTask: overload "Yes" at 00:00 = truncated start (NTFY 3-button cap)
# --------------------------------------------------------------------------


def _game_start_task(tmp_path, time_seconds=0):
    from video_grouper.task_processors.tasks.ntfy.game_start_task import GameStartTask

    group = tmp_path / "grp"
    group.mkdir(exist_ok=True)
    combined = group / "combined.mp4"
    combined.write_bytes(b"x")
    # Lightweight config/service — the task only reads config.ntfy.{topic,server_url,
    # enabled} for the buttons; the detector re-run is mocked in these tests.
    config = SimpleNamespace(
        ntfy=SimpleNamespace(topic="t", server_url="https://ntfy.sh", enabled=True),
        ttt=SimpleNamespace(),
    )
    to = f"{time_seconds // 60:02d}:{time_seconds % 60:02d}"
    return GameStartTask(
        str(group),
        config,
        MagicMock(),
        str(combined),
        time_offset=to,
        time_seconds=time_seconds,
    )


@pytest.mark.asyncio
async def test_game_start_yes_at_zero_triggers_truncated(tmp_path, monkeypatch):
    """'Yes' on the 00:00 frame -> truncated-start re-run (not the normal offset)."""
    task = _game_start_task(tmp_path, time_seconds=0)
    called = {}

    async def fake_resolve(group_dir, video, config, storage_path=None):
        called["args"] = (group_dir, video)
        return True

    monkeypatch.setattr(pgs, "resolve_truncated_start", fake_resolve)
    result = await task.process_response("Yes, already in progress at 00:00")
    assert called  # the truncated re-run was invoked
    assert result.should_continue is False
    assert result.metadata.get("truncated_start") is True
    assert result.metadata.get("start_time_offset") == "00:00"


@pytest.mark.asyncio
async def test_game_start_yes_after_zero_is_normal(tmp_path, monkeypatch):
    """'Yes' at a later frame keeps the normal walk behavior (offset = T - backup)."""
    task = _game_start_task(tmp_path, time_seconds=600)  # 10:00
    calls = {"n": 0}

    async def fake_resolve(*_a, **_k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(pgs, "resolve_truncated_start", fake_resolve)
    result = await task.process_response("Yes, game started at 10:00")
    assert calls["n"] == 0  # NOT the truncated path
    assert result.should_continue is False
    assert result.metadata.get("start_time_offset") == "06:00"  # 600 - 240s backup


@pytest.mark.asyncio
async def test_game_start_zero_question_is_truncation_framed(tmp_path):
    """The 00:00 question is phrased as the 'already in progress?' truncation check."""
    task = _game_start_task(tmp_path, time_seconds=0)
    task.generate_screenshot = _async_return(None)
    q = await task.create_question()
    assert "already in progress" in q["message"].lower()
    assert "Already started" in [a["label"] for a in q["actions"]]
