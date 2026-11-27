import configparser
import os
import tempfile

from video_grouper.models import MatchInfo


class TestMatchInfo:
    """Tests for the MatchInfo class."""

    def test_is_populated(self):
        """Test that is_populated returns the correct value."""
        # Create a populated MatchInfo object
        populated = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        assert populated.is_populated() is True

        # Create a partially populated MatchInfo object
        partially_populated = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        assert partially_populated.is_populated() is False

        # Create an unpopulated MatchInfo object
        unpopulated = MatchInfo(
            my_team_name="",
            opponent_team_name="",
            location="",
            start_time_offset="",
            total_duration="",
        )
        assert unpopulated.is_populated() is False

    def test_is_populated_treats_unknown_as_unpopulated(self):
        """`"Unknown"` is the NTFY fallback sentinel — it must not count as
        populated, otherwise populate_match_info_from_apis() will skip the
        PlayMetrics lookup and the real opponent never gets written."""
        for field, kwargs in [
            ("my_team_name", {"my_team_name": "Unknown"}),
            ("opponent_team_name", {"opponent_team_name": "Unknown"}),
            ("location", {"location": "Unknown"}),
        ]:
            defaults = {
                "my_team_name": "Team A",
                "opponent_team_name": "Team B",
                "location": "Home",
                "start_time_offset": "00:10:00",
                "total_duration": "01:30:00",
            }
            defaults.update(kwargs)
            mi = MatchInfo(**defaults)
            assert mi.is_populated() is False, (
                f"{field}='Unknown' should count as not populated"
            )

        # Case-insensitive: lowercase "unknown" and mixed case should also fail
        mi = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="unknown",
            location="Home",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        assert mi.is_populated() is False

        mi = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="UNKNOWN",
            location="Home",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        assert mi.is_populated() is False

    def test_get_team_info(self):
        """Test that get_team_info returns the correct dictionary."""
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        team_info = match_info.get_team_info()
        assert team_info == {
            "team_name": "Team A",
            "opponent_name": "Team B",
            "location": "Stadium",
        }

    def test_get_or_create(self):
        """Test that get_or_create returns a MatchInfo object and config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Test creating a new MatchInfo object
            match_info, config = MatchInfo.get_or_create(temp_dir)
            assert config is not None
            assert "MATCH" in config

            # Check that the file was created
            match_info_path = os.path.join(temp_dir, "match_info.ini")
            assert os.path.exists(match_info_path)

            # Test getting an existing MatchInfo object
            match_info, config = MatchInfo.get_or_create(temp_dir)
            assert config is not None
            assert "MATCH" in config

    def test_update_team_info(self):
        """Test that update_team_info updates the match_info.ini file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Update team info
            team_info = {
                "my_team_name": "Team A",
                "opponent_team_name": "Team B",
                "location": "Stadium",
            }
            match_info = MatchInfo.update_team_info(temp_dir, team_info)

            # Check that the file was created and updated
            match_info_path = os.path.join(temp_dir, "match_info.ini")
            assert os.path.exists(match_info_path)

            # Read the config directly to verify
            config = configparser.ConfigParser()
            config.read(match_info_path)
            assert config["MATCH"]["my_team_name"] == "Team A"
            assert config["MATCH"]["opponent_team_name"] == "Team B"
            assert config["MATCH"]["location"] == "Stadium"

            # Check the returned MatchInfo object
            assert match_info.my_team_name == "Team A"
            assert match_info.opponent_team_name == "Team B"
            assert match_info.location == "Stadium"

    def test_update_game_times(self):
        """Test that update_game_times updates the match_info.ini file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Update game times
            match_info = MatchInfo.update_game_times(
                temp_dir, start_time_offset="00:10:00", total_duration="01:30:00"
            )

            # Check that the file was created and updated
            match_info_path = os.path.join(temp_dir, "match_info.ini")
            assert os.path.exists(match_info_path)

            # Read the config directly to verify
            config = configparser.ConfigParser()
            config.read(match_info_path)
            assert config["MATCH"]["start_time_offset"] == "00:10:00"
            assert config["MATCH"]["total_duration"] == "01:30:00"

            # Check the returned MatchInfo object
            assert match_info.start_time_offset == "00:10:00"
            assert match_info.total_duration == "01:30:00"

    def test_from_file(self):
        """Test that from_file returns a MatchInfo object."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a match_info.ini file
            match_info_path = os.path.join(temp_dir, "match_info.ini")
            config = configparser.ConfigParser()
            config["MATCH"] = {
                "my_team_name": "Team A",
                "opponent_team_name": "Team B",
                "location": "Stadium",
                "start_time_offset": "00:10:00",
                "total_duration": "01:30:00",
            }
            with open(match_info_path, "w") as f:
                config.write(f)

            # Read the file
            match_info = MatchInfo.from_file(match_info_path)

            # Check the returned MatchInfo object
            assert match_info.my_team_name == "Team A"
            assert match_info.opponent_team_name == "Team B"
            assert match_info.location == "Stadium"
            assert match_info.start_time_offset == "00:10:00"
            assert match_info.total_duration == "01:30:00"

    def test_from_config(self):
        """Test that from_config returns a MatchInfo object."""
        config = configparser.ConfigParser()
        config["MATCH"] = {
            "my_team_name": "Team A",
            "opponent_team_name": "Team B",
            "location": "Stadium",
            "start_time_offset": "00:10:00",
            "total_duration": "01:30:00",
        }

        match_info = MatchInfo.from_config(config)

        # Check the returned MatchInfo object
        assert match_info.my_team_name == "Team A"
        assert match_info.opponent_team_name == "Team B"
        assert match_info.location == "Stadium"
        assert match_info.start_time_offset == "00:10:00"
        assert match_info.total_duration == "01:30:00"

    def test_get_total_duration_seconds_with_invalid_formats(self):
        """Test that get_total_duration_seconds handles invalid formats."""
        # Test with empty string
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="",
        )
        assert match_info.get_total_duration_seconds() == 90 * 60

        # Test with invalid format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="invalid",
        )
        assert match_info.get_total_duration_seconds() == 90 * 60

        # Test with valid MM:SS format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="45:00",
        )
        assert match_info.get_total_duration_seconds() == 45 * 60

        # Test with valid HH:MM:SS format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:10:00",
            total_duration="01:30:00",
        )
        assert match_info.get_total_duration_seconds() == 90 * 60

    def test_get_start_offset_with_invalid_formats(self):
        """Test that get_start_offset handles invalid formats."""
        # Test with empty string
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="",
            total_duration="01:30:00",
        )
        assert match_info.get_start_offset() == ""

        # Test with invalid format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="invalid",
            total_duration="01:30:00",
        )
        assert match_info.get_start_offset() == ""

        # Test with valid MM:SS format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="10:00",
            total_duration="01:30:00",
        )
        assert match_info.get_start_offset() == "00:10:00"

        # Test with valid HH:MM:SS format
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="01:10:00",
            total_duration="01:30:00",
        )
        assert match_info.get_start_offset() == "01:10:00"


class TestTTTGameIdSurvives:
    """match_info.ini ttt_game_id: kept by save(), cleared by a non-TTT re-match."""

    def _group(self, tmp_path):
        group_dir = tmp_path / "2026.12.05-10.00.00"
        group_dir.mkdir()
        return str(group_dir)

    def _read(self, group_dir):
        config = configparser.ConfigParser()
        config.read(os.path.join(group_dir, "match_info.ini"))
        return config["MATCH"]

    def test_save_keeps_the_ttt_game_id_and_other_keys(self, tmp_path):
        # The NTFY game-start confirmation calls save(); it must not erase the
        # TTT match written earlier by the schedule lookup.
        group_dir = self._group(tmp_path)
        MatchInfo.update_team_info(
            group_dir,
            {
                "my_team_name": "Flash",
                "opponent_team_name": "Rivals",
                "location": "Home",
                "ttt_game_id": "g-123",
            },
        )
        info, _ = MatchInfo.get_or_create(
            group_dir, os.path.dirname(os.path.abspath(group_dir))
        )
        info.group_dir = group_dir
        info.start_time_offset = "00:05:00"
        info.save()

        match = self._read(group_dir)
        assert match["ttt_game_id"] == "g-123"
        assert match["start_time_offset"] == "00:05:00"

    def test_a_non_ttt_rematch_clears_an_earlier_ttt_game_id(self, tmp_path):
        group_dir = self._group(tmp_path)
        MatchInfo.update_team_info(
            group_dir, {"opponent_team_name": "A", "ttt_game_id": "g-1"}
        )
        MatchInfo.update_team_info(
            group_dir, {"opponent_team_name": "B", "ttt_game_id": ""}
        )
        assert "ttt_game_id" not in self._read(group_dir)

    def test_writers_without_the_key_leave_it_alone(self, tmp_path):
        # NTFY team-info answers don't mention it; they must not clear it.
        group_dir = self._group(tmp_path)
        MatchInfo.update_team_info(
            group_dir, {"opponent_team_name": "A", "ttt_game_id": "g-1"}
        )
        MatchInfo.update_team_info(group_dir, {"location": "Away"})
        assert self._read(group_dir)["ttt_game_id"] == "g-1"
