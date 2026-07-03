"""TTT schedule cache and game auto-match for recordings."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from video_grouper.api_integrations.ttt_api import TTTApiClient
    from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class ScheduleService:
    """Fetches and caches the TTT team schedule, then matches recordings to games.

    All public methods are best-effort and never raise — errors are logged and
    safe return values (False / None) are returned instead.
    """

    def __init__(
        self,
        storage_path: str | Path,
        config: Config,
        ttt_client: TTTApiClient,
    ):
        self._storage_path = Path(storage_path)
        self._config = config
        self._ttt_client = ttt_client
        self._team_id: str | None = None
        self._team_name: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_team_id(self) -> str | None:
        """Resolve team_id from TTT assignments; cached after first call."""
        if self._team_id:
            return self._team_id
        try:
            assignments = self._ttt_client.get_team_assignments()
            if not assignments:
                return None
            if len(assignments) > 1:
                logger.debug(
                    "ScheduleService: %d team assignments; using first (team_id=%s)",
                    len(assignments),
                    assignments[0].get("team_id"),
                )
            team_id = assignments[0].get("team_id")
            if team_id:
                self._team_id = team_id
                # /device-link/me carries the TTT team_name — use it for the
                # {team}__... naming convention (config has no TTT team name).
                self._team_name = assignments[0].get("team_name") or None
            return team_id
        except Exception as exc:
            logger.error("ScheduleService: failed to resolve team_id: %s", exc)
            return None

    def _get_team_name(self) -> str:
        """Return the TTT team name resolved from the device-link assignment.

        Falls back to a ``config.ttt.team_name`` override if present, else "".
        """
        if self._team_name:
            return self._team_name
        try:
            ttt_cfg = getattr(self._config, "ttt", None)
            name = getattr(ttt_cfg, "team_name", None)
            if name:
                return str(name)
        except Exception:
            pass
        return ""

    def _cache_path(self, team_id: str) -> Path:
        return self._storage_path / "ttt" / f"schedule_{team_id}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> bool:
        """Fetch the schedule from TTT and write it to the local cache.

        Returns True on success, False on any error (never raises).
        Window: −30 days to +180 days from today.
        """
        team_id = self._resolve_team_id()
        if not team_id:
            return False
        try:
            today = datetime.now()
            start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
            end_date = (today + timedelta(days=180)).strftime("%Y-%m-%d")
            schedule = self._ttt_client.get_schedule(
                team_id,
                start_date=start_date,
                end_date=end_date,
            )
            ttt_dir = self._storage_path / "ttt"
            ttt_dir.mkdir(parents=True, exist_ok=True)
            dest = self._cache_path(team_id)
            # Atomic write via rename
            fd, tmp_path = tempfile.mkstemp(dir=str(ttt_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(schedule, fh)
                os.replace(tmp_path, dest)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.debug(
                "ScheduleService: wrote %d game(s) to %s",
                len(schedule or []),
                dest,
            )
            return True
        except Exception as exc:
            logger.error("ScheduleService: refresh failed: %s", exc)
            return False

    def find_game_for_recording(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any] | None:
        """Find the best TTT game that overlaps the recording window.

        Reads the local cache written by :meth:`refresh`; returns ``None``
        when the cache is absent, empty, or no game is close enough (per the
        2-hour proximity guard in
        :func:`~video_grouper.utils.game_selection.select_best_game`).

        Tags the returned dict with ``source="TTT"`` and ``team_name``.
        Never raises.
        """
        team_id = self._resolve_team_id()
        if not team_id:
            return None
        cache = self._cache_path(team_id)
        if not cache.exists():
            return None
        try:
            games: list[dict[str, Any]] = (
                json.loads(cache.read_text(encoding="utf-8")) or []
            )
        except Exception as exc:
            logger.warning("ScheduleService: could not read schedule cache: %s", exc)
            return None

        from video_grouper.utils.game_selection import select_best_game

        candidates = []
        for game in games:
            try:
                g_start_raw = game.get("start_time")
                g_end_raw = game.get("end_time")
                if not g_start_raw:
                    continue
                g_start = datetime.fromisoformat(g_start_raw)
                # Strip timezone so naive Reolink recording times compare cleanly
                if g_start.tzinfo is not None:
                    g_start = g_start.replace(tzinfo=None)
                if g_end_raw:
                    g_end = datetime.fromisoformat(g_end_raw)
                    if g_end.tzinfo is not None:
                        g_end = g_end.replace(tzinfo=None)
                else:
                    g_end = g_start + timedelta(hours=2)
                candidates.append((game, g_start, g_end))
            except (ValueError, TypeError) as exc:
                logger.debug(
                    "ScheduleService: skipping game with unparseable time: %s", exc
                )

        if not candidates:
            return None

        selected = select_best_game(
            candidates,
            start,
            end,
            game_label_fn=lambda g: g.get("id", "?"),
        )
        if selected is None:
            return None

        # Tag with source and team name; copy so we don't mutate the cached dict
        result = dict(selected)
        result["source"] = "TTT"
        result["team_name"] = self._get_team_name()
        return result
