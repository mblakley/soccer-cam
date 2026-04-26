"""Shared helper for stages that need a SecureLoader-acquired ONNX session.

Both :class:`DetectStage` and :class:`FieldMaskStage` (and any future
TTT-licensed-model stages) consume a model via the same flow:

1. Build a :class:`TTTApiClient` from ``ctx.ttt_config``.
2. Wrap it with :class:`SecureLoader`.
3. Call :meth:`SecureLoader.acquire` with a model key + channel + version.
4. Return the resulting :class:`InferenceSession`.

This module owns that flow once so the per-stage call sites stay short.
"""

from __future__ import annotations

import logging
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext

logger = logging.getLogger(__name__)


def acquire_session(
    *,
    model_key: str,
    channel: str | None,
    pipeline_version: str | None,
    ctx: ProviderContext,
    log_label: str,
) -> Any:
    """Acquire a license + decrypted ONNX session from TTT.

    ``log_label`` prefixes the success log line so each stage shows its
    own tag (e.g. ``"detect"`` or ``"field_mask"``).
    """
    from video_grouper.api_integrations.ttt_api import TTTApiClient
    from video_grouper.ball_tracking.secure_loader import SecureLoader

    if not ctx.ttt_config:
        raise RuntimeError(
            f"{log_label}: model_key is set but TTT integration is disabled "
            "(set [TTT] enabled = true and configure credentials, "
            "or fall back to model_path for local testing)"
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
        log_label,
        loaded.model_key,
        loaded.version,
        loaded.tier,
        loaded.provider,
    )
    return loaded.session
