"""Shared helper for steps that run a TTT-licensed model.

Any step whose config carries a ``model_key`` acquires its model the same
way: build a :class:`TTTApiClient` from the step context's ``ttt_config``,
run the :class:`SecureLoader` against it, and use the decrypted in-memory
session. This module holds that one path so each step doesn't re-implement
it (``detect`` and ``field_detect`` today).

Import is cheap — TTT client and loader are imported lazily inside the
function, so pulling this in doesn't drag the network stack into bundles
that never license a model.
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
    """Acquire a license + decrypted ONNX session from TTT.

    Constructs a TTTApiClient from ``ctx.ttt_config`` and runs the SecureLoader
    against it. Returns the loaded ``InferenceSession``. ``step_name`` only
    scopes the log/error messages.
    """
    from video_grouper.api_integrations.ttt_api import TTTApiClient
    from video_grouper.ball_tracking.secure_loader import SecureLoader

    if not ctx.ttt_config:
        raise RuntimeError(
            f"{step_name}: model_key is set but TTT integration is disabled "
            "(set [TTT] enabled = true and configure credentials, or fall back "
            "to model_path for a community / bring-your-own model)"
        )

    cfg = ctx.ttt_config
    client = TTTApiClient(
        supabase_url=cfg.get("supabase_url", ""),
        anon_key=cfg.get("anon_key", ""),
        api_base_url=cfg.get("api_base_url", ""),
        storage_path=str(ctx.storage_path),
    )
    public_keys = cfg.get("plugin_signing_public_keys") or []
    loader = SecureLoader(client, public_keys, state_storage_path=str(ctx.storage_path))
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
