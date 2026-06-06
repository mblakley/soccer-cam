"""Deterministic-inference helpers used by the iOS-port parity harness.

The harness runs detect/track/render with these settings so two consecutive
runs over the same input produce byte-identical detections.json,
trajectory.json, and per-frame artifacts. The Swift port on iOS then has a
checked-in reference to validate against.

Production runs do NOT use these helpers — the default CUDA/DirectML EPs are
faster and acceptably reproducible for shipping output, but their floating-
point ordering is non-deterministic across runs / hardware, which would mask
real port regressions in a parity test.
"""

from __future__ import annotations

import logging
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import onnxruntime as ort

logger = logging.getLogger(__name__)


PARITY_PROVIDERS: tuple[str, ...] = ("CPUExecutionProvider",)
"""The execution-provider tuple used by the parity harness.

CPU EP is the only provider with byte-stable floating-point output across
runs of the same ONNX Runtime build. GPU EPs (CUDA, DirectML) use
non-deterministic reductions that drift by a few LSBs run-to-run."""


def make_deterministic_sess_options() -> "ort.SessionOptions":
    """Return ONNX session options configured for run-to-run reproducibility.

    - Sequential execution (no inter-op parallelism reordering)
    - Single intra-op thread (eliminates thread-scheduling float drift)
    - Memory arena disabled (different allocator paths can perturb fp accum)
    """
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.enable_cpu_mem_arena = False
    opts.enable_mem_pattern = False
    return opts


def seed_everything(seed: int = 1337) -> None:
    """Seed Python, NumPy, and PYTHONHASHSEED for parity-harness runs.

    The detector + tracker don't currently use randomness (Kalman has fixed
    init), but seeding keeps us safe against future additions and makes the
    determinism contract explicit at the harness boundary.
    """
    import numpy as np

    random.seed(seed)
    np.random.seed(seed)
    os.environ.setdefault("PYTHONHASHSEED", str(seed))
    logger.debug("parity harness: seeded everything with %d", seed)
