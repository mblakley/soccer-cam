"""Base contract every ball-tracking provider must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProviderContext:
    """Runtime context passed to :meth:`BallTrackingProvider.run`.

    Attributes:
        group_dir: Absolute path to the game's video-group directory.
        team_name: Team identifier (e.g. ``"flash"``, ``"heat"``) or
            ``None`` if unknown. Used by per-team overrides upstream.
        storage_path: Absolute path to soccer-cam's shared-data root.
    """

    group_dir: Path
    team_name: str | None
    storage_path: Path


class BallTrackingProvider(ABC):
    """A provider takes an unprocessed panoramic video and writes a
    pan-and-scan broadcast-style output.

    Implementations register themselves at import time via
    :func:`video_grouper.ball_tracking.register_provider`.
    """

    @abstractmethod
    async def run(
        self, input_path: str, output_path: str, ctx: ProviderContext
    ) -> bool:
        """Process *input_path* into *output_path*.

        Args:
            input_path: Panoramic source video.
            output_path: Where to write the broadcast-style output.
            ctx: Runtime context (game dir, team name, storage root).

        Returns:
            ``True`` on success, ``False`` on failure. Implementations
            should not raise on expected failures — log + return ``False``.
        """
