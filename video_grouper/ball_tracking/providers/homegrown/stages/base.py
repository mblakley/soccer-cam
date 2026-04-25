"""Base contract every homegrown processing stage must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.ball_tracking.config import HomegrownProviderConfig


class ProcessingStage(ABC):
    """One phase of the homegrown pipeline.

    Stages are constructed once per provider invocation with the parent
    :class:`HomegrownProviderConfig`. Each stage's :meth:`run` receives
    the live ``artifacts`` dict (see :mod:`provider` for the conventional
    keys) and returns a dict of updates to merge in. Returning ``None``
    means "no changes".

    Concrete subclasses set the class-level ``name`` to their registry
    key (matching the string in ``config.ball_tracking.HOMEGROWN.stages``).
    """

    name: ClassVar[str]

    def __init__(self, provider_config: HomegrownProviderConfig):
        self.provider_config = provider_config

    @abstractmethod
    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None: ...
