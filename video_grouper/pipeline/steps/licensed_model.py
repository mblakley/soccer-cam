"""Shared helper for steps that run a TTT-licensed model.

Any step whose config carries a ``model_key`` acquires its model the same
way: run the :class:`SecureLoader` against the step context's entitlements
port and use the decrypted in-memory session. This module holds that one path
so each step doesn't re-implement it (``detect`` and ``field_detect`` today).

Import is cheap -- the loader is imported lazily inside the function.
"""

from __future__ import annotations

import logging
from typing import Any

from video_grouper.pipeline.base import StepContext

logger = logging.getLogger(__name__)


def build_secure_loader_session(
    step_name: str,
    model_key: str,
    channel: str | None,
    pipeline_version: str | None,
    ctx: StepContext,
) -> Any:
    """Acquire a licence + decrypted ONNX session through the entitlements port.

    Returns the loaded ``InferenceSession``. ``step_name`` only scopes the
    log/error messages.
    """
    from video_grouper.ball_tracking.secure_loader import SecureLoader

    entitlements = ctx.entitlements
    if entitlements is None or not entitlements.available:
        raise RuntimeError(
            f"{step_name}: model_key {model_key!r} is a licensed model, but no "
            "entitlements provider is signed in. Sign in to Team Tech Tools "
            "from the dashboard, or use model_path for a community / "
            "bring-your-own model."
        )

    loader = SecureLoader(
        entitlements,
        ctx.signing_public_keys,
        state_storage_path=str(ctx.storage_path),
    )
    loaded = loader.acquire(
        model_key, channel=channel, pipeline_version=pipeline_version
    )
    logger.info(
        "%s: licensed %s v%s (%s, provider=%s)",
        step_name,
        loaded.model_key,
        loaded.version,
        loaded.tier,
        loaded.provider,
    )
    return loaded.session
