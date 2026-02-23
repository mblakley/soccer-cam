"""
Tests for the shared game selection logic (proximity guard + midpoint heuristic).
"""

from datetime import datetime, timedelta, timezone

from video_grouper.utils.game_selection import select_best_game


def _utc(*args):
    """Create a UTC-aware datetime."""
    return datetime(*args, tzinfo=timezone.utc)


class TestSelectBestGame:
    """Tests for select_best_game()."""

    def test_empty_candidates_returns_none(self):
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        assert select_best_game([], rec_start, rec_end) is None

    def test_single_nearby_game_returns_it(self):
        """Single candidate within proximity -> returned."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        game = {"name": "Game A"}
        g_start = _utc(2024, 6, 15, 10, 15)
        g_end = _utc(2024, 6, 15, 11, 15)

        result = select_best_game([(game, g_start, g_end)], rec_start, rec_end)
        assert result is game

    def test_distant_game_rejected_by_proximity_guard(self):
        """Game whose midpoint is >2 hours from recording midpoint -> None."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        # Game starts 5 hours later
        game = {"name": "Distant Game"}
        g_start = _utc(2024, 6, 15, 15, 0)
        g_end = _utc(2024, 6, 15, 16, 0)

        result = select_best_game([(game, g_start, g_end)], rec_start, rec_end)
        assert result is None

    def test_game_at_exactly_2h_boundary_accepted(self):
        """Game whose midpoint is exactly 2 hours away -> accepted (<=)."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        # Recording midpoint = 10:30. Game midpoint must be at 12:30 (exactly 2h).
        game = {"name": "Boundary Game"}
        g_start = _utc(2024, 6, 15, 12, 0)
        g_end = _utc(2024, 6, 15, 13, 0)

        result = select_best_game([(game, g_start, g_end)], rec_start, rec_end)
        assert result is game

    def test_game_just_beyond_2h_boundary_rejected(self):
        """Game whose midpoint is just over 2 hours away -> rejected."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        # Recording midpoint = 10:30. Game midpoint at 12:31 (>2h).
        game = {"name": "Too Far Game"}
        g_start = _utc(2024, 6, 15, 12, 1)
        g_end = _utc(2024, 6, 15, 13, 1)

        result = select_best_game([(game, g_start, g_end)], rec_start, rec_end)
        assert result is None

    def test_two_games_midpoint_in_range_picks_closest(self):
        """Two games pass proximity, both midpoints in recording range -> closest wins."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 12, 0)
        # Recording midpoint = 11:00

        game_a = {"name": "Game A"}
        # Game A: 10:30-11:30, midpoint=11:00 (distance=0)
        ga_start = _utc(2024, 6, 15, 10, 30)
        ga_end = _utc(2024, 6, 15, 11, 30)

        game_b = {"name": "Game B"}
        # Game B: 10:00-12:00, midpoint=11:00 (distance=0, but same...)
        # Let's make B's midpoint at 11:15 (distance=15min)
        gb_start = _utc(2024, 6, 15, 10, 30)
        gb_end = _utc(2024, 6, 15, 12, 0)
        # Game B midpoint = 11:15, distance = 15 min

        result = select_best_game(
            [(game_a, ga_start, ga_end), (game_b, gb_start, gb_end)],
            rec_start,
            rec_end,
        )
        assert result is game_a

    def test_two_games_one_midpoint_in_range_prefers_it(self):
        """One game's midpoint is within recording range, the other is outside."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        # Recording midpoint = 10:30

        # Game A: midpoint outside recording range but within 2h proximity
        game_a = {"name": "Game A"}
        ga_start = _utc(2024, 6, 15, 11, 0)
        ga_end = _utc(2024, 6, 15, 12, 0)
        # Game A midpoint = 11:30, outside [10:00, 11:00] but within 2h

        # Game B: midpoint inside recording range
        game_b = {"name": "Game B"}
        gb_start = _utc(2024, 6, 15, 10, 0)
        gb_end = _utc(2024, 6, 15, 11, 0)
        # Game B midpoint = 10:30, inside [10:00, 11:00]

        result = select_best_game(
            [(game_a, ga_start, ga_end), (game_b, gb_start, gb_end)],
            rec_start,
            rec_end,
        )
        assert result is game_b

    def test_e2e_scenario_group1_gets_eagles(self):
        """
        E2E scenario: 2 games, Group 1 recording (0:00-3:00) -> Eagles.

        Game 1 (Eagles): -0:30 to 3:30, midpoint at 1:30
        Game 2 (Falcons): 2:30 to 6:40, midpoint at 4:35
        Group 1 recording: 0:00 to 3:00, midpoint at 1:30
        -> Game 1 midpoint (1:30) is within [0:00, 3:00] and closer to 1:30
        """
        base = _utc(2024, 6, 15, 12, 0)

        rec_start = base
        rec_end = base + timedelta(minutes=3)

        game1 = {"name": "Eagles"}
        g1_start = base - timedelta(seconds=30)
        g1_end = base + timedelta(minutes=3, seconds=30)

        game2 = {"name": "Falcons"}
        g2_start = base + timedelta(minutes=2, seconds=30)
        g2_end = base + timedelta(minutes=6, seconds=40)

        result = select_best_game(
            [(game1, g1_start, g1_end), (game2, g2_start, g2_end)],
            rec_start,
            rec_end,
        )
        assert result is game1

    def test_e2e_scenario_group2_gets_falcons(self):
        """
        E2E scenario: 2 games, Group 2 recording (3:10-6:10) -> Falcons.

        Game 1 (Eagles): -0:30 to 3:30, midpoint at 1:30
        Game 2 (Falcons): 2:30 to 6:40, midpoint at 4:35
        Group 2 recording: 3:10 to 6:10, midpoint at 4:40
        -> Game 2 midpoint (4:35) is within [3:10, 6:10] and closer to 4:40
        """
        base = _utc(2024, 6, 15, 12, 0)

        rec_start = base + timedelta(minutes=3, seconds=10)
        rec_end = base + timedelta(minutes=6, seconds=10)

        game1 = {"name": "Eagles"}
        g1_start = base - timedelta(seconds=30)
        g1_end = base + timedelta(minutes=3, seconds=30)

        game2 = {"name": "Falcons"}
        g2_start = base + timedelta(minutes=2, seconds=30)
        g2_end = base + timedelta(minutes=6, seconds=40)

        result = select_best_game(
            [(game1, g1_start, g1_end), (game2, g2_start, g2_end)],
            rec_start,
            rec_end,
        )
        assert result is game2

    def test_no_games_returns_none(self):
        """No games at all -> None."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        assert select_best_game([], rec_start, rec_end) is None

    def test_fallback_closest_midpoint_when_none_in_range(self):
        """Both midpoints outside recording range -> fallback picks closest overall."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 10, 30)
        # Recording midpoint = 10:15

        # Game A: midpoint at 10:45 (30 min away, outside [10:00, 10:30])
        game_a = {"name": "Game A"}
        ga_start = _utc(2024, 6, 15, 10, 30)
        ga_end = _utc(2024, 6, 15, 11, 0)

        # Game B: midpoint at 11:00 (45 min away, outside [10:00, 10:30])
        game_b = {"name": "Game B"}
        gb_start = _utc(2024, 6, 15, 10, 45)
        gb_end = _utc(2024, 6, 15, 11, 15)

        result = select_best_game(
            [(game_a, ga_start, ga_end), (game_b, gb_start, gb_end)],
            rec_start,
            rec_end,
        )
        assert result is game_a

    def test_game_label_fn_used_for_logging(self):
        """Verify game_label_fn is called without errors."""
        rec_start = _utc(2024, 6, 15, 10, 0)
        rec_end = _utc(2024, 6, 15, 11, 0)
        game = {"title": "My Game"}
        g_start = _utc(2024, 6, 15, 10, 15)
        g_end = _utc(2024, 6, 15, 11, 15)

        result = select_best_game(
            [(game, g_start, g_end)],
            rec_start,
            rec_end,
            game_label_fn=lambda g: g["title"],
        )
        assert result is game
