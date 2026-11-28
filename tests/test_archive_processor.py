"""Tests for the archive step.

The ordering guarantee is the thing under test: copy -> verify -> record ->
delete. An earlier ad-hoc version deleted first and recorded nothing, and an
interrupted run left a group that the pipeline read as mid-processing and
began reprocessing — a game already published on YouTube.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.task_processors.archive_processor import ArchiveProcessor
from video_grouper.task_processors.tasks.archive import ArchiveTask
from video_grouper.utils.config import (
    AppConfig,
    ArchiveConfig,
    AutocamConfig,
    CameraConfig,
    CloudSyncConfig,
    Config,
    LoggingConfig,
    NtfyConfig,
    PlayMetricsConfig,
    ProcessingConfig,
    RecordingConfig,
    StorageConfig,
    TeamConfig,
    TeamSnapConfig,
    YouTubeConfig,
    load_config,
)
from video_grouper.utils.paths import get_camera_state_path

# The live archive roots, as they exist on the server (verified 2026-08-07).
# Raw strings: these are literal Windows paths, and "F:\Heat_2012s" would be
# read as an escape sequence.
HEAT_2012 = r"F:\Heat_2012s"
HEAT_2013 = r"F:\Heat_2013s"
FLASH_2013 = r"F:\Flash_2013s"
FALLBACK = r"F:\archive"


@pytest.fixture(autouse=True)
def real_filesystem(mock_file_system):
    """Undo conftest's global filesystem mocks for this module.

    These tests copy, hash and delete actual bytes — the point is that the
    ordering holds against a real disk. The autouse ``mock_file_system``
    fixture makes ``os.path.exists`` always True, ``getsize`` always 1 MB and
    ``makedirs`` a no-op, which would make every assertion here meaningless.
    """
    mock_file_system["exists"].side_effect = os.path.lexists
    mock_file_system["getsize"].side_effect = lambda p: os.stat(p).st_size
    mock_file_system["makedirs"].side_effect = lambda p, *a, **kw: Path(p).mkdir(
        parents=True, exist_ok=True
    )
    mock_file_system["access"].side_effect = lambda *a, **kw: True
    return mock_file_system


@pytest.fixture
def archive_root(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    return str(root)


@pytest.fixture
def archive_config(temp_storage):
    """A real Config — conftest's mock_config is a ConfigParser stand-in."""
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="127.0.0.1",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        archive=ArchiveConfig(),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=False, username="u", password="p", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
    )


async def _make_group(
    storage,
    name,
    *,
    status="complete",
    newest=None,
    payload=b"x" * 4096,
    with_recording=True,
    camera_name=None,
):
    """A completed group with one real video file on disk.

    ``camera_name`` is recorded in the recording's metadata, as the poller
    does at discovery; None leaves it unattributed.
    """
    group_dir = os.path.join(storage, name)
    # Path.mkdir, not os.makedirs — conftest patches the latter to a no-op.
    Path(group_dir).mkdir(parents=True, exist_ok=True)
    video = os.path.join(group_dir, "game.mp4")
    Path(video).write_bytes(payload)

    newest = newest or datetime(2026, 7, 12, 10, 35, 15)
    dir_state = DirectoryState(group_dir)
    if with_recording:
        await dir_state.add_file(
            video,
            RecordingFile(
                start_time=newest - timedelta(minutes=30),
                end_time=newest,
                file_path=video,
                status="complete",
                metadata={"camera_name": camera_name} if camera_name else None,
            ),
        )
    await dir_state.update_group_status(status)

    match_info = os.path.join(group_dir, "match_info.ini")
    Path(match_info).write_text(
        "[MATCH]\nmy_team_name = Heat\nopponent_team_name = Kenmore\nlocation = Away\n",
        encoding="utf-8",
    )
    return group_dir


WATERMARK_PAST = datetime(2026, 7, 12, 10, 35, 15)


def _configure_cameras(config, *names):
    config.cameras = [
        CameraConfig(
            name=name,
            type="dahua",
            device_ip="127.0.0.1",
            username="admin",
            password="password",
        )
        for name in names
    ]


def _write_camera_state(storage, state):
    Path(get_camera_state_path(storage)).write_text(json.dumps(state), encoding="utf-8")


def _archived_copy(archive_root, folder="2026.07.12 - vs Kenmore (away)"):
    return os.path.join(archive_root, folder, "game.mp4")


class TestArchiveOrdering:
    @pytest.mark.asyncio
    async def test_group_is_recorded_archived_before_anything_is_deleted(
        self, temp_storage, archive_root
    ):
        """The interruption that nearly re-published a game.

        If the process dies mid-delete, whatever survives must still read as
        finished. That only holds if the terminal status is written first.
        """
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        task = ArchiveTask(group_dir=group_dir)

        recorded_when_first_delete_happened = {}
        real_remove = os.remove

        def spy_remove(path, *a, **kw):
            # Ignore FileLock's own .lock cleanup — that is bookkeeping, not
            # a deletion of the game's content.
            if not str(path).endswith(".lock") and (
                "status" not in recorded_when_first_delete_happened
            ):
                state_file = os.path.join(group_dir, "state.json")
                with open(state_file) as fh:
                    recorded_when_first_delete_happened["status"] = json.load(fh).get(
                        "status"
                    )
            return real_remove(path, *a, **kw)

        os.remove = spy_remove
        try:
            ok = await task.execute(
                archive_root=archive_root,
                watermark=datetime(2026, 7, 12, 10, 35, 15),
            )
        finally:
            os.remove = real_remove

        assert ok
        assert recorded_when_first_delete_happened["status"] == "archived", (
            "status must already be 'archived' before the first file is removed"
        )

    @pytest.mark.asyncio
    async def test_verified_copy_lands_and_local_space_is_reclaimed(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=datetime(2026, 7, 12, 10, 35, 15)
        )

        assert ok
        assert not os.path.exists(group_dir)
        archived = os.path.join(
            archive_root, "2026.07.12 - vs Kenmore (away)", "game.mp4"
        )
        assert os.path.exists(archived)
        assert os.path.getsize(archived) == 4096

    @pytest.mark.asyncio
    async def test_copy_disposition_keeps_the_local_copy(
        self, temp_storage, archive_root
    ):
        """`copy`: a second copy exists, the working drive is untouched."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            make_second_copy=True,
            reclaim_local_space=False,
            watermark=datetime(2026, 7, 12, 10, 35, 15),
        )

        assert ok
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == "archived"

    @pytest.mark.asyncio
    async def test_rerunning_an_archived_group_without_reclaim_is_a_no_op(
        self, temp_storage, archive_root
    ):
        """`copy` already happened; with nothing to reclaim there is nothing
        left to do. (This used to be asserted for `move` too — which is the
        bug that left a half-deleted group occupying the disk forever; see
        TestFinishInterruptedReclaim.)"""
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", status="archived"
        )

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            reclaim_local_space=False,
            watermark=WATERMARK_PAST,
        )

        assert ok
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert not os.path.exists(_archived_copy(archive_root))


class TestWatermarkGuard:
    @pytest.mark.asyncio
    async def test_refuses_to_archive_ahead_of_the_watermark(
        self, temp_storage, archive_root
    ):
        """Archiving a game the poller has not finished with would let it be
        rediscovered as new and downloaded again — the whole bug."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            # Watermark is BEHIND the group's newest recording.
            watermark=datetime(2026, 7, 12, 9, 0, 0),
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == "complete", (
            "a refused archive must not mark the group archived"
        )

    @pytest.mark.asyncio
    async def test_refuses_when_there_is_no_watermark_at_all(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=None
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))

    @pytest.mark.asyncio
    async def test_processor_takes_the_slowest_camera_watermark(
        self, temp_storage, archive_config
    ):
        """With several cameras, only the slowest one's mark is safe: a fast
        camera must not license deleting footage another is still behind on."""
        _configure_cameras(archive_config, "fast_cam", "slow_cam")
        _write_camera_state(
            temp_storage,
            {
                "is_connected": False,  # legacy top-level key, must be skipped
                "fast_cam": {"latest_video_time": "2026-07-12 10:35:15"},
                "slow_cam": {"latest_video_time": "2026-07-01 08:00:00"},
            },
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark() == datetime(2026, 7, 1, 8, 0, 0)

    @pytest.mark.asyncio
    async def test_a_removed_camera_does_not_block_archiving(
        self, temp_storage, archive_config
    ):
        """Regression: the minimum was taken over every entry in
        camera_state.json, so a camera removed or renamed in config left its
        last mark behind and pinned the guard there forever."""
        _configure_cameras(archive_config, "default")
        _write_camera_state(
            temp_storage,
            {
                "default": {"latest_video_time": "2026-07-12 10:35:15"},
                "old_camera": {"latest_video_time": "2025-01-01 00:00:00"},
            },
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark() == datetime(2026, 7, 12, 10, 35, 15)

    @pytest.mark.asyncio
    async def test_a_configured_camera_with_no_mark_refuses(
        self, temp_storage, archive_config
    ):
        """A camera that has never settled anything has not finished with any
        time range; its absence must not be read as permission."""
        _configure_cameras(archive_config, "default", "new_cam")
        _write_camera_state(
            temp_storage, {"default": {"latest_video_time": "2026-07-12 10:35:15"}}
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark() is None

    @pytest.mark.asyncio
    async def test_only_the_cameras_that_recorded_the_group_are_asked(
        self, temp_storage, archive_config
    ):
        """A group recorded by one camera is not held back by another camera
        that never saw it (say, one not yet plugged in)."""
        _configure_cameras(archive_config, "default", "new_cam")
        _write_camera_state(
            temp_storage, {"default": {"latest_video_time": "2026-07-12 10:35:15"}}
        )
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", camera_name="default"
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark(
            ArchiveTask(group_dir=group_dir).recording_cameras()
        ) == datetime(2026, 7, 12, 10, 35, 15)


class TestArchiveIsOptIn:
    @pytest.mark.asyncio
    async def test_disabled_by_default_does_nothing(self, temp_storage, archive_config):
        assert archive_config.archive.after_upload == "keep"
        processor = ArchiveProcessor(temp_storage, archive_config)
        task = Mock(spec=ArchiveTask)
        task.group_dir = os.path.join(temp_storage, "grp")
        task.execute = AsyncMock()

        await processor.process_item(task)
        task.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_archive_path_refuses_rather_than_guessing(
        self, temp_storage
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root="",
            make_second_copy=True,
            watermark=datetime(2026, 7, 12, 10, 35, 15),
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))


class TestSingleDriveDiscard:
    """`discard`: reclaim local space with no second location.

    The case the earlier design ignored. An install with one drive has
    nowhere to copy to, and is exactly the one that fills up — YouTube is
    its archive.
    """

    @pytest.mark.asyncio
    async def test_discard_reclaims_space_without_an_archive_path(self, temp_storage):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        dir_state = DirectoryState(group_dir)
        dir_state.record_uploaded_video("processed", "8zQkiP3vHYk")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root="",
            make_second_copy=False,
            reclaim_local_space=True,
            watermark=datetime(2026, 7, 12, 10, 35, 15),
        )

        assert ok
        assert not os.path.exists(group_dir)

    @pytest.mark.asyncio
    async def test_discard_refuses_without_a_recorded_youtube_id(self, temp_storage):
        """These are the only files that exist. Reaching `complete` is not
        proof YouTube has the game — a recorded video id is."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root="",
            make_second_copy=False,
            reclaim_local_space=True,
            watermark=datetime(2026, 7, 12, 10, 35, 15),
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))

    @pytest.mark.asyncio
    async def test_discard_still_respects_the_watermark(self, temp_storage):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        dir_state = DirectoryState(group_dir)
        dir_state.record_uploaded_video("processed", "8zQkiP3vHYk")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root="",
            make_second_copy=False,
            reclaim_local_space=True,
            watermark=datetime(2026, 7, 12, 9, 0, 0),
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))


def _config_with(after_upload="move", path="", teams=None):
    """A minimal real Config: an [ARCHIVE] section plus [TEAM.<key>] teams.

    ``teams`` maps a handle to TeamConfig kwargs, exactly as a
    ``[TEAM.<key>]`` section would hold them.
    """
    return Config(
        storage=StorageConfig(path="unused"),
        archive=ArchiveConfig(after_upload=after_upload, path=path),
        teams={key: TeamConfig(**kw) for key, kw in (teams or {}).items()},
    )


class TestDispositionConfig:
    def test_keep_is_the_default(self):
        cfg = ArchiveConfig()
        assert cfg.after_upload == "keep"
        assert not cfg.makes_second_copy
        assert not cfg.reclaims_local_space

    @pytest.mark.parametrize(
        "mode,second_copy,reclaims",
        [
            ("keep", False, False),
            ("copy", True, False),
            ("move", True, True),
            ("discard", False, True),
        ],
    )
    def test_disposition_matrix(self, mode, second_copy, reclaims):
        cfg = ArchiveConfig(after_upload=mode, path="F:/archive")  # noqa: E501
        assert cfg.makes_second_copy is second_copy
        assert cfg.reclaims_local_space is reclaims

    @pytest.mark.parametrize("mode", ["copy", "move"])
    def test_second_copy_without_a_path_is_refused_at_config_load(self, mode):
        """Hard-fail rather than degrade to a no-op: an operator who asked for
        a second copy and silently got none finds out only once the working
        drive is already gone."""
        with pytest.raises(ValueError, match="needs somewhere to put games"):
            _config_with(after_upload=mode)

    @pytest.mark.parametrize("mode", ["copy", "move"])
    def test_a_team_archive_path_alone_satisfies_the_requirement(self, mode):
        """A team's archive_path is somewhere to put games; no `path` needed."""
        cfg = _config_with(
            after_upload=mode,
            teams={"heat2012": {"name": "Guzzetta", "archive_path": HEAT_2012}},
        )
        assert cfg.archive_root_for("Guzzetta") == HEAT_2012

    @pytest.mark.parametrize("mode", ["keep", "discard"])
    def test_pathless_dispositions_are_valid(self, mode):
        assert _config_with(after_upload=mode).archive.path == ""


class TestPerTeamArchiveRoots:
    """Archives are per-team, and the root is not derivable from the name.

    Verified against the live layout on 2026-08-07: Heat_2012s, Heat_2013s
    and Flash_2013s under F:, holding folders named
    "2026.07.12 - vs Niagara Falls Soccer Club (away)". The team recorded in
    match_info is "Guzzetta" while its root is Heat_2012s, and the two Heat
    age groups must never be mixed — so the root is configured, not
    inferred: ``archive_path`` in the team's own ``[TEAM.<key>]`` section,
    resolved by ``Config.team_for`` like every other per-team setting.
    """

    def test_team_maps_to_its_own_root(self):
        cfg = _config_with(
            teams={
                "heat2012": {"name": "Guzzetta", "archive_path": HEAT_2012},
                "heat2013": {"name": "Heat 2013", "archive_path": HEAT_2013},
                "flash": {"name": "Flash", "archive_path": FLASH_2013},
            },
        )
        assert cfg.archive_root_for("Guzzetta") == HEAT_2012
        assert cfg.archive_root_for("Heat 2013") == HEAT_2013
        assert cfg.archive_root_for("Flash") == FLASH_2013

    def test_lookup_survives_case_and_stray_whitespace(self):
        """match_info is hand-edited."""
        cfg = _config_with(
            teams={"heat2012": {"name": "guzzetta", "archive_path": HEAT_2012}}
        )
        assert cfg.archive_root_for("  Guzzetta  ") == HEAT_2012

    def test_unmapped_team_falls_back_to_path(self):
        cfg = _config_with(
            path=FALLBACK,
            teams={"flash": {"name": "Flash", "archive_path": FLASH_2013}},
        )
        assert cfg.archive_root_for("Someone Else") == FALLBACK

    def test_known_team_without_an_archive_path_falls_back_to_path(self):
        cfg = _config_with(
            path=FALLBACK,
            teams={
                "flash": {"name": "Flash", "archive_path": FLASH_2013},
                "heat2012": {"name": "Guzzetta"},
            },
        )
        assert cfg.archive_root_for("Guzzetta") == FALLBACK

    def test_unmapped_team_with_no_fallback_has_nowhere_to_go(self):
        cfg = _config_with(
            teams={"flash": {"name": "Flash", "archive_path": FLASH_2013}}
        )
        assert cfg.archive_root_for("Guzzetta") == ""

    def test_archive_path_is_read_from_a_team_section(self, tmp_path):
        """Regression: per-team roots were a separate [ARCHIVE.PER_TEAM] map
        with its own matching rule; the team's own section had no field for
        it, so an archive_path written there was silently ignored."""
        ini = tmp_path / "config.ini"
        ini.write_text(
            "[STORAGE]\npath = unused\n"
            "[ARCHIVE]\nafter_upload = move\n"
            "[TEAM.heat2012]\nname = BU14 - Guzzetta\naliases = Guzzetta\n"
            f"archive_path = {HEAT_2012}\n",
            encoding="utf-8",
        )

        cfg = load_config(ini)

        assert cfg.archive_root_for("BU14 - Guzzetta") == HEAT_2012

    def test_a_leftover_archive_per_team_section_refuses_to_load(self, tmp_path):
        """Pre-release builds read [ARCHIVE.PER_TEAM]. Ignoring it would send
        every game to the fallback root (or nowhere) without a word."""
        ini = tmp_path / "config.ini"
        ini.write_text(
            "[STORAGE]\npath = unused\n"
            f"[ARCHIVE]\nafter_upload = move\npath = {FALLBACK}\n"
            f"[ARCHIVE.PER_TEAM]\nguzzetta = {HEAT_2012}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"ARCHIVE\.PER_TEAM"):
            load_config(ini)

    @pytest.mark.asyncio
    async def test_game_lands_directly_in_its_team_root(
        self, temp_storage, archive_root
    ):
        """Matches the existing layout: <root>/<date> - vs <opponent> (away),
        with NO team component appended — the root is already team-specific."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        assert ArchiveTask(group_dir=group_dir).destination(
            archive_root
        ) == os.path.join(archive_root, "2026.07.12 - vs Kenmore (away)")

    @pytest.mark.asyncio
    async def test_processor_refuses_a_team_with_no_root(
        self, temp_storage, archive_config
    ):
        """Filing a game under another team's root is not something a later
        pass can untangle, so refuse instead of guessing."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        archive_config.archive.after_upload = "move"
        archive_config.archive.path = ""
        archive_config.teams = {
            "flash": TeamConfig(name="Flash", archive_path=FLASH_2013)
        }

        processor = ArchiveProcessor(temp_storage, archive_config)
        with pytest.raises(RuntimeError, match="no archive root for team 'Heat'"):
            await processor.process_item(ArchiveTask(group_dir=group_dir))

        assert os.path.exists(os.path.join(group_dir, "game.mp4"))


class TestRootLookupMatchesRealTeamNames:
    """The archive root is chosen from a game's my_team_name.

    That value is the full registered name — on the live install
    "BU14 - Guzzetta", not "Guzzetta" — while an operator writes the short
    handle they think of the team by. ``resolve_team`` matches exactly, then
    by substring with the longest identifier winning, for every per-team
    setting; the archive root uses it rather than a rule of its own.
    """

    def test_the_real_my_team_name_resolves_from_a_short_alias(self):
        cfg = _config_with(
            teams={"heat2012": {"name": "guzzetta", "archive_path": HEAT_2012}}
        )

        assert cfg.archive_root_for("BU14 - Guzzetta") == HEAT_2012

    def test_both_live_teams_resolve(self):
        cfg = _config_with(
            teams={
                "heat2012": {"name": "guzzetta", "archive_path": HEAT_2012},
                "flash": {"name": "flash", "archive_path": FLASH_2013},
            },
        )

        assert cfg.archive_root_for("BU14 - Guzzetta") == HEAT_2012
        assert (
            cfg.archive_root_for("Western New York Flash - 13B ECNL-RL Rochester")
            == FLASH_2013
        )

    def test_longest_identifier_wins_so_age_groups_are_not_mixed(self):
        """Filing a game under another team's root then deleting the original
        is not something a later pass can undo."""
        cfg = _config_with(
            teams={
                "flash": {"name": "flash", "archive_path": FLASH_2013},
                "rochester": {
                    "name": "wny flash rochester",
                    "archive_path": HEAT_2013,
                },
            },
        )

        assert cfg.archive_root_for("WNY Flash Rochester ECNL") == HEAT_2013

    def test_an_unmapped_team_still_has_nowhere_to_go(self):
        """The refusal must survive: a near-miss must not fall back silently."""
        cfg = _config_with(
            teams={"heat2012": {"name": "guzzetta", "archive_path": HEAT_2012}}
        )

        assert cfg.archive_root_for("Some Other Club") == ""

    def test_path_is_still_the_fallback_when_configured(self):
        cfg = _config_with(
            path=FALLBACK,
            teams={"heat2012": {"name": "guzzetta", "archive_path": HEAT_2012}},
        )

        assert cfg.archive_root_for("Some Other Club") == FALLBACK

    @pytest.mark.asyncio
    async def test_a_real_game_archives_instead_of_being_refused(
        self, temp_storage, archive_root, archive_config
    ):
        """End to end: the group's match_info says "Heat", the team section
        names "heat", and the archive must actually run."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        archive_config.archive.after_upload = "move"
        archive_config.archive.path = ""
        archive_config.teams = {
            "heat": TeamConfig(name="heat", archive_path=archive_root)
        }

        processor = ArchiveProcessor(temp_storage, archive_config)
        Path(get_camera_state_path(temp_storage)).write_text(
            json.dumps({"default": {"latest_video_time": "2026-07-12 10:35:15"}}),
            encoding="utf-8",
        )

        await processor.process_item(ArchiveTask(group_dir=group_dir))

        assert not os.path.exists(group_dir)
        assert os.path.exists(
            os.path.join(archive_root, "2026.07.12 - vs Kenmore (away)", "game.mp4")
        )


class TestRefusesAGroupItCannotVouchFor:
    """Every refusal here is a hard failure: nothing copied, nothing deleted,
    nothing recorded."""

    @pytest.mark.asyncio
    async def test_relative_group_dir_is_refused(
        self, temp_storage, archive_root, tmp_path, monkeypatch
    ):
        """Regression: the upload step handed over a group_dir relative to the
        storage path. Resolved against the process's working directory it
        named nothing, the walk found no files, and the task recorded an
        empty "archived" group there -- reporting success."""
        await _make_group(temp_storage, "2026.07.12-10.00.00")
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        with pytest.raises(ValueError, match="absolute"):
            await ArchiveTask(group_dir="2026.07.12-10.00.00").execute(
                archive_root=archive_root, watermark=WATERMARK_PAST
            )

        assert list(elsewhere.iterdir()) == [], "nothing may be written in CWD"
        assert os.listdir(archive_root) == []

    @pytest.mark.asyncio
    async def test_nonexistent_group_dir_is_refused(self, temp_storage, archive_root):
        missing = os.path.join(temp_storage, "2026.07.12-10.00.00")

        with pytest.raises(ValueError, match="does not exist"):
            await ArchiveTask(group_dir=missing).execute(
                archive_root=archive_root, watermark=WATERMARK_PAST
            )

        assert not os.path.exists(missing)
        assert os.listdir(archive_root) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["pending", "combined", "trimmed", "pipeline_complete"]
    )
    @pytest.mark.parametrize("reclaim", [True, False], ids=["move", "copy"])
    async def test_a_group_that_is_not_complete_is_refused(
        self, temp_storage, archive_root, status, reclaim
    ):
        """Regression: the task never checked the group had reached
        `complete`, so a queued task for an unfinished group would copy a
        partial game, mark it terminally `archived` and -- for move/discard --
        delete footage that was never published."""
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", status=status
        )

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            reclaim_local_space=reclaim,
            watermark=WATERMARK_PAST,
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == status
        assert os.listdir(archive_root) == []

    @pytest.mark.asyncio
    async def test_reclaim_refused_when_no_recording_has_an_end_time(
        self, temp_storage, archive_root
    ):
        """Regression: with no recording end time the watermark guard was
        skipped entirely, so the one check standing between "delete" and "the
        poller rediscovers this game as new" silently did not run."""
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", with_recording=False
        )

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == "complete"


class TestDestinationIsUniquePerGroup:
    """Two games on one day against one opponent -- a tournament, a
    double-header -- produce the same "<date> - vs <opp> (away)" name."""

    @pytest.mark.asyncio
    async def test_second_game_never_overwrites_the_firsts_archive(
        self, temp_storage, archive_root
    ):
        """Regression: the second game's copy landed in the first game's
        folder. A larger file was copied straight over the first game's only
        remaining copy (its local files already reclaimed); an equal-sized one
        failed its hash check on every retry, forever."""
        first = await _make_group(
            temp_storage, "2026.07.12-09.00.00", payload=b"A" * 4096
        )
        second = await _make_group(
            temp_storage, "2026.07.12-10.00.00", payload=b"B" * 8192
        )

        assert await ArchiveTask(group_dir=first).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )
        assert await ArchiveTask(group_dir=second).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        first_copy = Path(_archived_copy(archive_root))
        second_copy = Path(
            _archived_copy(archive_root, "2026.07.12 - vs Kenmore (away) (2)")
        )
        assert first_copy.read_bytes() == b"A" * 4096, "first game's copy destroyed"
        assert second_copy.read_bytes() == b"B" * 8192

    @pytest.mark.asyncio
    async def test_same_size_second_game_archives_instead_of_failing_forever(
        self, temp_storage, archive_root
    ):
        first = await _make_group(
            temp_storage, "2026.07.12-09.00.00", payload=b"A" * 4096
        )
        second = await _make_group(
            temp_storage, "2026.07.12-10.00.00", payload=b"B" * 4096
        )

        assert await ArchiveTask(group_dir=first).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )
        assert await ArchiveTask(group_dir=second).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )
        assert not os.path.exists(second)

    @pytest.mark.asyncio
    async def test_an_unmarked_existing_folder_is_left_alone(
        self, temp_storage, archive_root
    ):
        """A same-named folder nothing claims (an earlier hand-run archive) is
        not ours to write into."""
        foreign = Path(archive_root, "2026.07.12 - vs Kenmore (away)")
        foreign.mkdir()
        (foreign / "game.mp4").write_bytes(b"F" * 9000)
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert (foreign / "game.mp4").read_bytes() == b"F" * 9000
        assert (
            os.path.getsize(
                _archived_copy(archive_root, "2026.07.12 - vs Kenmore (away) (2)")
            )
            == 4096
        )

    @pytest.mark.asyncio
    async def test_a_resumed_archive_reuses_its_own_folder(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            reclaim_local_space=False,
            watermark=WATERMARK_PAST,
        )
        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert sorted(os.listdir(archive_root)) == ["2026.07.12 - vs Kenmore (away)"]

    @pytest.mark.asyncio
    async def test_a_corrupt_copy_of_our_own_is_recopied_once(
        self, temp_storage, archive_root
    ):
        """Regression: a same-size destination whose bytes differ (a torn
        write from an interrupted run) failed verification on every retry,
        forever. It is our own folder, so it is re-copied once and verified
        again."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            reclaim_local_space=False,
            watermark=WATERMARK_PAST,
        )
        Path(_archived_copy(archive_root)).write_bytes(b"\0" * 4096)

        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert Path(_archived_copy(archive_root)).read_bytes() == b"x" * 4096
        assert not os.path.exists(group_dir)


class TestFinishInterruptedReclaim:
    """`archived` is written before anything is deleted. A delete that then
    fails (a file locked by a player or the indexer) must not leave the group
    on disk forever."""

    @pytest.mark.asyncio
    async def test_retry_finishes_a_reclaim_that_failed_partway(
        self, temp_storage, archive_root, monkeypatch
    ):
        """Regression: the retry saw `archived` and returned "already
        archived" without deleting anything, so the space was never
        reclaimed."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        video = os.path.join(group_dir, "game.mp4")
        real_remove = os.remove

        def locked(path, *a, **kw):
            if os.path.abspath(path) == os.path.abspath(video):
                raise PermissionError("file is locked")
            return real_remove(path, *a, **kw)

        monkeypatch.setattr(os, "remove", locked)
        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )
        assert ok is False
        assert DirectoryState(group_dir).status == "archived"
        assert os.path.exists(video)

        monkeypatch.setattr(os, "remove", real_remove)
        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert ok
        assert not os.path.exists(group_dir)
        assert Path(_archived_copy(archive_root)).read_bytes() == b"x" * 4096

    @pytest.mark.asyncio
    async def test_reclaim_of_an_archived_group_reverifies_first(
        self, temp_storage, archive_root
    ):
        """`archived` alone is not proof the copy is still there. Finishing a
        reclaim goes back through copy+verify, so a copy lost since is
        restored before the local files go."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            reclaim_local_space=False,
            watermark=WATERMARK_PAST,
        )
        os.remove(_archived_copy(archive_root))

        assert await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=WATERMARK_PAST
        )

        assert Path(_archived_copy(archive_root)).read_bytes() == b"x" * 4096
        assert not os.path.exists(group_dir)
