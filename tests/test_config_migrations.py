"""Tests for versioned config.ini migrations.

The framework's job is to make a schema change apply exactly once and leave no
permanent legacy-reading code behind. These tests pin the three properties that
makes true: migrations run in order from whatever version a file is at, running
them twice changes nothing, and a file from a newer build is refused rather
than quietly mangled.
"""

import configparser

import pytest

from video_grouper.utils import config_migrations as cm
from video_grouper.utils.config import load_config

# The per-team sections exactly as the live server config spells them,
# including configparser's lowercasing of the playlist-map keys and the short
# substring spellings that only resolve because the lookup is fuzzy.
LIVE_CONFIG = """\
[STORAGE]
path = D:\\soccer-cam-storage

[TEAMSNAP]
enabled = True
client_id = tg_abc

[TEAMSNAP.TEAM.0]
enabled = True
team_id = 10198718
team_name = BU14 - Guzzetta

[PLAYMETRICS]
enabled = True
username = mark@example.com

[PLAYMETRICS.TEAM.0]
team_id = 335774
team_name = Western New York Flash - 13B ECNL-RL Rochester
enabled = True

[YOUTUBE.PLAYLIST_MAP]
13b ecnl-rl rochester = WNY Flash 2013s
guzzetta = Hilton Heat 2012s
"""


def _write(tmp_path, text, name="config.ini"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _sections(path):
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return {s: dict(parser.items(s)) for s in parser.sections()}


class TestFramework:
    def test_unversioned_file_runs_every_migration(self, tmp_path):
        path = _write(tmp_path, LIVE_CONFIG)
        applied = cm.migrate_config_file(path)
        assert [m.version for m in applied] == [m.version for m in cm.MIGRATIONS]

    def test_version_is_stamped_and_second_run_is_a_no_op(self, tmp_path):
        path = _write(tmp_path, LIVE_CONFIG)
        cm.migrate_config_file(path)

        after_first = path.read_text(encoding="utf-8")
        assert _sections(path)[cm.VERSION_SECTION][cm.VERSION_OPTION] == str(
            cm.current_schema_version()
        )

        assert cm.migrate_config_file(path) == []
        assert path.read_text(encoding="utf-8") == after_first

    def test_original_is_kept_as_a_backup(self, tmp_path):
        path = _write(tmp_path, LIVE_CONFIG)
        cm.migrate_config_file(path)
        backup = path.with_suffix(path.suffix + ".bak")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == LIVE_CONFIG

    def test_a_partly_migrated_file_only_runs_what_it_still_needs(self, tmp_path):
        """The 'between x and y' case: a file already past v1 skips it."""
        path = _write(
            tmp_path,
            f"{LIVE_CONFIG}\n[{cm.VERSION_SECTION}]\n{cm.VERSION_OPTION} = 1\n",
        )
        applied = cm.migrate_config_file(path)
        assert [m.version for m in applied] == [
            m.version for m in cm.MIGRATIONS if m.version > 1
        ]

    def test_a_file_from_a_newer_build_is_refused(self, tmp_path):
        """An older binary cannot know what a later migration did."""
        future = cm.current_schema_version() + 5
        path = _write(
            tmp_path,
            f"{LIVE_CONFIG}\n[{cm.VERSION_SECTION}]\n{cm.VERSION_OPTION} = {future}\n",
        )
        with pytest.raises(ValueError, match="newer soccer-cam"):
            cm.migrate_config_file(path)

    def test_unreadable_version_is_treated_as_unversioned(self, tmp_path):
        """Better to re-run migrations (they are idempotent) than skip them."""
        path = _write(
            tmp_path,
            f"{LIVE_CONFIG}\n[{cm.VERSION_SECTION}]\n{cm.VERSION_OPTION} = banana\n",
        )
        assert cm.migrate_config_file(path)

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert cm.migrate_config_file(tmp_path / "nope.ini") == []

    def test_migration_versions_are_ordered_and_contiguous(self):
        """A gap or a repeat would silently skip or re-run a migration."""
        versions = [m.version for m in cm.MIGRATIONS]
        assert versions == list(range(1, len(versions) + 1))


class TestPerTeamConsolidation:
    """v2: five scattered per-team sections become one [TEAM.<key>] each."""

    @pytest.fixture
    def migrated(self, tmp_path):
        path = _write(tmp_path, LIVE_CONFIG)
        cm.migrate_config_file(path)
        return load_config(path)

    def test_both_real_teams_survive_with_their_ids(self, migrated):
        heat = migrated.team_for("BU14 - Guzzetta")
        flash = migrated.team_for("Western New York Flash - 13B ECNL-RL Rochester")

        assert heat is not None and flash is not None
        assert heat.teamsnap_team_id == "10198718"
        assert flash.playmetrics_team_id == "335774"

    def test_playlists_are_folded_in_across_the_spelling_mismatch(self, migrated):
        """The playlist map is keyed 'guzzetta'; the team is 'BU14 - Guzzetta'.

        Only substring matching ever connected those two, so the migration has
        to reproduce it rather than compare the strings.
        """
        assert migrated.team_for("BU14 - Guzzetta").youtube_playlist == (
            "Hilton Heat 2012s"
        )
        assert (
            migrated.team_for(
                "Western New York Flash - 13B ECNL-RL Rochester"
            ).youtube_playlist
            == "WNY Flash 2013s"
        )

    def test_the_short_playlist_key_is_kept_as_an_alias(self, migrated):
        """It is what the operator wrote, and it still has to resolve."""
        heat = migrated.team_for("BU14 - Guzzetta")
        assert "guzzetta" in [a.casefold() for a in heat.aliases]
        assert migrated.team_for("guzzetta") is heat

    def test_legacy_sections_are_gone_from_disk(self, tmp_path):
        """The point of migrating rather than shimming: the old shape is gone."""
        path = _write(tmp_path, LIVE_CONFIG)
        cm.migrate_config_file(path)
        sections = _sections(path)

        assert not [s for s in sections if s.startswith("TEAMSNAP.")]
        assert not [s for s in sections if s.startswith("PLAYMETRICS.TEAM.")]
        assert "YOUTUBE.PLAYLIST_MAP" not in sections
        assert [s for s in sections if s.startswith("TEAM.")]

    def test_credentials_are_left_alone(self, migrated):
        """Only the per-team parts move; the integration sections stay."""
        assert migrated.teamsnap.client_id == "tg_abc"
        assert migrated.playmetrics.username == "mark@example.com"

    def test_a_config_with_no_teams_migrates_cleanly(self, tmp_path):
        path = _write(tmp_path, "[STORAGE]\npath = D:\\x\n")
        cm.migrate_config_file(path)
        config = load_config(path)
        assert config.teams == {}
        assert config.team_for("Anyone") is None


class TestBallTrackingMigration:
    """v1: the shim that used to run on every single load, now run once."""

    def test_legacy_ball_tracking_becomes_a_pipeline(self, tmp_path):
        path = _write(
            tmp_path,
            "[STORAGE]\npath = D:\\x\n\n"
            "[BALL_TRACKING]\nprovider = autocam_gui\nenabled = true\n\n"
            "[BALL_TRACKING.AUTOCAM_GUI]\nexecutable = C:/once/AutocamGUI.exe\n",
        )
        cm.migrate_config_file(path)

        config = load_config(path)
        assert [s.type for s in config.pipeline.ordered_steps()] == ["autocam"]
        assert "BALL_TRACKING" not in _sections(path)

    def test_an_explicit_pipeline_is_not_overwritten(self, tmp_path):
        """`[PIPELINE] enabled = false` is a deliberate choice, not an absence."""
        path = _write(
            tmp_path,
            "[STORAGE]\npath = D:\\x\n\n"
            "[BALL_TRACKING]\nprovider = autocam_gui\nenabled = true\n\n"
            "[PIPELINE]\nenabled = false\nsteps =\n",
        )
        cm.migrate_config_file(path)

        config = load_config(path)
        assert config.pipeline.enabled is False
        assert config.pipeline.ordered_steps() == []


class TestPerTeamPipelineFinallyWorks:
    """Per-team pipeline selection had two independent bugs and never fired.

    `[PIPELINE.PER_TEAM]` round-tripped through config but `ordered_steps`
    ignored its team argument, AND the team name it was given was always None
    because pipeline discovery read `[MATCH] team_name` while match_info only
    ever writes `my_team_name`. Both are fixed, so the setting means something
    for the first time.
    """

    PIPELINE_INI = """\
[STORAGE]
path = /data

[PIPELINE]
enabled = true
steps = stitch, ball_detect, render

[PIPELINE.stitch]
type = stitch_correct

[PIPELINE.ball_detect]
type = ball_detect

[PIPELINE.render]
type = render

[TEAM.heat]
name = BU14 - Guzzetta
pipeline = stitch, render

[TEAM.flash]
name = WNY Flash
"""

    def test_a_team_can_override_which_steps_run(self, tmp_path):
        config = load_config(_write(tmp_path, self.PIPELINE_INI))
        team = config.team_for("BU14 - Guzzetta")

        steps = config.pipeline.ordered_steps(
            "BU14 - Guzzetta", override_steps=team.pipeline
        )

        assert [s.step_id for s in steps] == ["stitch", "render"]

    def test_a_team_without_an_override_runs_the_shared_pipeline(self, tmp_path):
        config = load_config(_write(tmp_path, self.PIPELINE_INI))
        team = config.team_for("WNY Flash")

        steps = config.pipeline.ordered_steps("WNY Flash", override_steps=team.pipeline)

        assert [s.step_id for s in steps] == ["stitch", "ball_detect", "render"]

    def test_discovery_reads_my_team_name_not_team_name(self, tmp_path):
        """The option is `my_team_name`; reading `team_name` always gave None."""
        from video_grouper.task_processors.pipeline_discovery_processor import (
            PipelineDiscoveryProcessor,
        )

        group = tmp_path / "2026.07.12-10.00.00"
        group.mkdir()
        (group / "match_info.ini").write_text(
            "[MATCH]\nmy_team_name = BU14 - Guzzetta\nopponent_team_name = X\n",
            encoding="utf-8",
        )

        assert PipelineDiscoveryProcessor._read_team_name(group) == "BU14 - Guzzetta"


class TestIntegrationsSurviveMigration:
    """The migration deletes the sections TeamSnap/PlayMetrics read from.

    Their `teams` lists are projected back out of [TEAM.*] at load time. Without
    that, an upgraded install would silently find no teams and both
    integrations would quietly stop fetching schedules.
    """

    def test_team_lists_are_identical_before_and_after(self, tmp_path):
        path = _write(tmp_path, LIVE_CONFIG)
        before = load_config(path)
        cm.migrate_config_file(path)
        after = load_config(path)

        def summarise(config):
            return (
                sorted((t.team_name, t.team_id) for t in config.teamsnap.teams),
                sorted((t.team_name, t.team_id) for t in config.playmetrics.teams),
            )

        assert summarise(after) == summarise(before)
        assert summarise(after)[0] == [("BU14 - Guzzetta", "10198718")]

    def test_a_disabled_team_is_not_projected(self, tmp_path):
        config = load_config(
            _write(
                tmp_path,
                "[STORAGE]\npath = /data\n\n[TEAMSNAP]\nenabled = true\n\n"
                "[TEAM.heat]\nname = BU14 - Guzzetta\n"
                "teamsnap_team_id = 10198718\nenabled = false\n",
            )
        )
        assert config.teamsnap.teams == []


class TestResolverAmbiguity:
    """With several teams, the best match must win — not the first configured."""

    AMBIGUOUS = """\
[STORAGE]
path = /data

[TEAM.generic]
name = Flash
youtube_playlist = Generic Flash

[TEAM.specific]
name = WNY Flash Rochester
youtube_playlist = WNY Flash 2013s
"""

    def test_longest_match_wins_regardless_of_config_order(self, tmp_path):
        config = load_config(_write(tmp_path, self.AMBIGUOUS))

        team = config.team_for("WNY Flash Rochester ECNL")

        assert team is not None
        assert team.youtube_playlist == "WNY Flash 2013s", (
            "the more specific team must win; first-configured would misfile "
            "the game into the other team's playlist"
        )

    def test_exact_match_beats_a_longer_substring_match(self, tmp_path):
        config = load_config(_write(tmp_path, self.AMBIGUOUS))
        assert config.team_for("Flash").youtube_playlist == "Generic Flash"

    def test_a_disabled_team_never_matches(self, tmp_path):
        config = load_config(
            _write(
                tmp_path,
                "[STORAGE]\npath = /data\n\n"
                "[TEAM.off]\nname = Flash\nenabled = false\n",
            )
        )
        assert config.team_for("Flash") is None


class TestUnreadableConfigIsLeftAlone:
    """A file we cannot parse must not be replaced by a version stub.

    configparser ignores unreadable paths silently rather than raising, so
    without a guard an empty or missing file reads as "no sections", which
    looks identical to "unversioned" — and the migration would back it up and
    write a config containing nothing but [SCHEMA].
    """

    def test_empty_file_is_not_migrated(self, tmp_path):
        path = _write(tmp_path, "")

        assert cm.migrate_config_file(path) == []
        assert path.read_text(encoding="utf-8") == ""
        assert not path.with_suffix(path.suffix + ".bak").exists()

    def test_a_file_with_only_comments_is_not_migrated(self, tmp_path):
        path = _write(tmp_path, "# nothing but a comment\n")

        assert cm.migrate_config_file(path) == []
        assert "SCHEMA" not in path.read_text(encoding="utf-8")
