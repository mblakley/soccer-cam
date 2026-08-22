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
    TeamSnapConfig,
    YouTubeConfig,
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
    storage, name, *, status="complete", newest=None, payload=b"x" * 4096
):
    """A completed group with one real video file on disk."""
    group_dir = os.path.join(storage, name)
    # Path.mkdir, not os.makedirs — conftest patches the latter to a no-op.
    Path(group_dir).mkdir(parents=True, exist_ok=True)
    video = os.path.join(group_dir, "game.mp4")
    Path(video).write_bytes(payload)

    newest = newest or datetime(2026, 7, 12, 10, 35, 15)
    dir_state = DirectoryState(group_dir)
    await dir_state.add_file(
        video,
        RecordingFile(
            start_time=newest - timedelta(minutes=30),
            end_time=newest,
            file_path=video,
            status="complete",
        ),
    )
    await dir_state.update_group_status(status)

    match_info = os.path.join(group_dir, "match_info.ini")
    Path(match_info).write_text(
        "[MATCH]\nmy_team_name = Heat\nopponent_team_name = Kenmore\nlocation = Away\n",
        encoding="utf-8",
    )
    return group_dir


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
    async def test_rerunning_an_archived_group_is_a_no_op(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", status="archived"
        )

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=datetime(2026, 7, 12, 10, 35, 15)
        )

        assert ok
        assert os.path.exists(os.path.join(group_dir, "game.mp4")), (
            "an already-archived group must not be touched again"
        )


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
        state_path = get_camera_state_path(temp_storage)
        Path(state_path).write_text(
            json.dumps(
                {
                    "is_connected": False,  # legacy top-level key, must be skipped
                    "fast_cam": {"latest_video_time": "2026-07-12 10:35:15"},
                    "slow_cam": {"latest_video_time": "2026-07-01 08:00:00"},
                }
            ),
            encoding="utf-8",
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark() == datetime(2026, 7, 1, 8, 0, 0)


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
            ArchiveConfig(after_upload=mode)

    @pytest.mark.parametrize("mode", ["copy", "move"])
    def test_per_team_map_alone_satisfies_the_requirement(self, mode):
        """A per-team map is somewhere to put games; no `path` needed."""
        cfg = ArchiveConfig(after_upload=mode, PER_TEAM={"Guzzetta": HEAT_2012})
        assert cfg.root_for_team("Guzzetta") == HEAT_2012

    @pytest.mark.parametrize("mode", ["keep", "discard"])
    def test_pathless_dispositions_are_valid(self, mode):
        assert ArchiveConfig(after_upload=mode).path == ""


class TestPerTeamArchiveRoots:
    """Archives are per-team, and the root is not derivable from the name.

    Verified against the live layout on 2026-08-07: Heat_2012s, Heat_2013s
    and Flash_2013s under F:, holding folders named
    "2026.07.12 - vs Niagara Falls Soccer Club (away)". The team recorded in
    match_info is "Guzzetta" while its root is Heat_2012s, and the two Heat
    age groups must never be mixed — so the mapping is configured, not
    inferred.
    """

    def test_team_maps_to_its_own_root(self):
        cfg = ArchiveConfig(
            after_upload="move",
            PER_TEAM={
                "Guzzetta": HEAT_2012,
                "Heat 2013": HEAT_2013,
                "Flash": FLASH_2013,
            },
        )
        assert cfg.root_for_team("Guzzetta") == HEAT_2012
        assert cfg.root_for_team("Heat 2013") == HEAT_2013
        assert cfg.root_for_team("Flash") == FLASH_2013

    def test_lookup_survives_configparser_lowercasing_and_stray_whitespace(self):
        """configparser lowercases option keys, and match_info is hand-edited."""
        cfg = ArchiveConfig(after_upload="move", PER_TEAM={"guzzetta": HEAT_2012})
        assert cfg.root_for_team("  Guzzetta  ") == HEAT_2012

    def test_unmapped_team_falls_back_to_path(self):
        cfg = ArchiveConfig(
            after_upload="move", path=FALLBACK, PER_TEAM={"Flash": FLASH_2013}
        )
        assert cfg.root_for_team("Someone Else") == FALLBACK

    def test_unmapped_team_with_no_fallback_has_nowhere_to_go(self):
        cfg = ArchiveConfig(after_upload="move", PER_TEAM={"Flash": FLASH_2013})
        assert cfg.root_for_team("Guzzetta") == ""

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
        archive_config.archive.per_team = {"Flash": FLASH_2013}

        processor = ArchiveProcessor(temp_storage, archive_config)
        with pytest.raises(RuntimeError, match="no archive root for team 'Heat'"):
            await processor.process_item(ArchiveTask(group_dir=group_dir))

        assert os.path.exists(os.path.join(group_dir, "game.mp4"))


class TestRootLookupMatchesRealTeamNames:
    """The archive root is chosen from a game's my_team_name.

    That value is the full registered name — on the live install
    "BU14 - Guzzetta", not "Guzzetta" — while an operator writes the short
    handle they think of the team by. An exact match misses, and archiving
    refuses every game with "no archive root for team". Substring matching is
    the same rule [YOUTUBE.PLAYLIST_MAP] has always used.
    """

    def test_the_real_my_team_name_resolves_from_a_short_key(self):
        cfg = ArchiveConfig(after_upload="move", PER_TEAM={"guzzetta": HEAT_2012})

        assert cfg.root_for_team("BU14 - Guzzetta") == HEAT_2012

    def test_both_live_teams_resolve(self):
        cfg = ArchiveConfig(
            after_upload="move",
            PER_TEAM={"guzzetta": HEAT_2012, "flash": FLASH_2013},
        )

        assert cfg.root_for_team("BU14 - Guzzetta") == HEAT_2012
        assert (
            cfg.root_for_team("Western New York Flash - 13B ECNL-RL Rochester")
            == FLASH_2013
        )

    def test_longest_key_wins_so_age_groups_are_not_mixed(self):
        """Filing a game under another team's root then deleting the original
        is not something a later pass can undo."""
        cfg = ArchiveConfig(
            after_upload="move",
            PER_TEAM={"flash": FLASH_2013, "wny flash rochester": HEAT_2013},
        )

        assert cfg.root_for_team("WNY Flash Rochester ECNL") == HEAT_2013

    def test_an_unmapped_team_still_has_nowhere_to_go(self):
        """The refusal must survive: a near-miss must not fall back silently."""
        cfg = ArchiveConfig(after_upload="move", PER_TEAM={"guzzetta": HEAT_2012})

        assert cfg.root_for_team("Some Other Club") == ""

    def test_path_is_still_the_fallback_when_configured(self):
        cfg = ArchiveConfig(
            after_upload="move", path=FALLBACK, PER_TEAM={"guzzetta": HEAT_2012}
        )

        assert cfg.root_for_team("Some Other Club") == FALLBACK

    @pytest.mark.asyncio
    async def test_a_real_game_archives_instead_of_being_refused(
        self, temp_storage, archive_root, archive_config
    ):
        """End to end: the group's match_info says "Heat", the operator
        configured "heat", and the archive must actually run."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        archive_config.archive.after_upload = "move"
        archive_config.archive.path = ""
        archive_config.archive.per_team = {"heat": archive_root}

        processor = ArchiveProcessor(temp_storage, archive_config)
        Path(get_camera_state_path(temp_storage)).write_text(
            json.dumps({"cam": {"latest_video_time": "2026-07-12 10:35:15"}}),
            encoding="utf-8",
        )

        await processor.process_item(ArchiveTask(group_dir=group_dir))

        assert not os.path.exists(group_dir)
        assert os.path.exists(
            os.path.join(archive_root, "2026.07.12 - vs Kenmore (away)", "game.mp4")
        )
