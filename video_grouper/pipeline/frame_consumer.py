"""Per-frame consumer plugins for the ``frame_fanout`` step.

A :class:`FrameConsumer` processes a stream of *already-decoded* frames. It exists so
the expensive source decode (e.g. an 8K-wide HEVC) happens ONCE while many consumers —
several render variants, or a detector alongside a render — each receive every decoded
frame in lockstep, in memory. The fan-out step owns the decode loop and drives the
consumers; the consumers own their per-frame work and their own outputs.

This is the in-memory counterpart to :class:`~video_grouper.pipeline.base.PipelineStep`:
steps hand off via the filesystem (resumable, fingerprinted) at step boundaries, while a
fan-out step shares decoded frames in memory *within* its boundary. Consumers register
themselves at import time, mirroring the step registry (so they're pluggable the same way
steps are).

Kept dependency-light (numpy + pydantic only at module top) so it imports in every bundle;
heavy imports (av/cv2/onnx) live inside concrete consumers.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    import numpy as np

    from video_grouper.pipeline.base import StepContext
    from video_grouper.pipeline.manifest import PipelineManifest


@dataclass(frozen=True)
class FrameSourceInfo:
    """Decoded-source properties the fan-out step shares with every consumer (so a render
    consumer can size + time-base its output encoder identically to the source)."""

    width: int
    height: int
    average_rate: Any  # av Fraction
    time_base: Any  # av Fraction


class FrameConsumer(ABC):
    """One per-frame consumer driven by the ``frame_fanout`` step.

    Lifecycle: ``open`` once (after the source dimensions + resolved geometry are known),
    ``consume`` per decoded frame, ``close`` once (flush + record outputs). Declares the
    manifest artifact keys it reads (:attr:`consumes`) and writes (:attr:`produces`) so the
    fan-out step can aggregate them into its own ``consumes``/``produces`` for the runner.
    """

    config_model: ClassVar[type[BaseModel]]
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()

    def __init__(self, config: BaseModel):
        self.config = config

    @abstractmethod
    def open(
        self,
        source: FrameSourceInfo,
        ctx: StepContext,
        manifest: PipelineManifest,
    ) -> None:
        """Prepare to consume given the decoded ``source`` (read inputs from the manifest,
        resolve any per-consumer geometry, open outputs)."""

    @abstractmethod
    def consume(self, rgb: np.ndarray, frame_pts: int, frame_idx: int) -> None:
        """Process one decoded RGB frame."""

    @abstractmethod
    def close(self, manifest: PipelineManifest) -> None:
        """Flush/close outputs and record produced paths via ``manifest.put``."""


# Registry: name -> (module, class, config_module, config_class), resolved at call time so
# importing this module is cheap and PyInstaller bundles the consumer modules via the
# static imports in their step module (e.g. render.py).
_CONSUMER_REGISTRY: dict[str, tuple[str, str, str, str]] = {}


def register_frame_consumer(
    name: str, consumer_class: type[FrameConsumer], config_class: type[BaseModel]
) -> None:
    """Register a frame-consumer implementation under *name* (last writer wins)."""
    _CONSUMER_REGISTRY[name] = (
        consumer_class.__module__,
        consumer_class.__name__,
        config_class.__module__,
        config_class.__name__,
    )


def get_frame_consumer_config_class(name: str) -> type[BaseModel]:
    """Return the registered config model for consumer *name*."""
    if name not in _CONSUMER_REGISTRY:
        raise ValueError(
            f"unknown frame consumer {name!r}. Available: {', '.join(sorted(_CONSUMER_REGISTRY)) or '(none)'}"
        )
    _, _, c_module, c_class = _CONSUMER_REGISTRY[name]
    return getattr(importlib.import_module(c_module), c_class)


def create_frame_consumer(name: str, config: BaseModel | dict) -> FrameConsumer:
    """Instantiate the consumer registered under *name* (validating a raw dict config)."""
    if name not in _CONSUMER_REGISTRY:
        raise ValueError(
            f"unknown frame consumer {name!r}. Available: {', '.join(sorted(_CONSUMER_REGISTRY)) or '(none)'}"
        )
    s_module, s_class, c_module, c_class = _CONSUMER_REGISTRY[name]
    if not isinstance(config, BaseModel):
        cfg_cls = getattr(importlib.import_module(c_module), c_class)
        config = cfg_cls.model_validate(config)
    consumer_cls = getattr(importlib.import_module(s_module), s_class)
    return consumer_cls(config=config)
