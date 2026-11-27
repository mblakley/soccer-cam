"""Team Tech Tools entitlements: capabilities, model licences and plugins for
the signed-in account. Chosen whenever TTT is enabled; ``available`` follows the
shared session, so signing in later takes effect without a restart."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import EntitlementUnavailable, IEntitlements

logger = logging.getLogger(__name__)


@provides(IEntitlements)
class TTTEntitlements(IEntitlements):
    name = "ttt"
    priority = 10

    def __init__(self, client: Any):
        self._client = client

    @classmethod
    def from_context(cls, context) -> TTTEntitlements | None:
        return cls(context.ttt_client()) if context.ttt_enabled() else None

    @property
    def available(self) -> bool:
        return bool(self._client.is_authenticated())

    def licensee(self) -> str | None:
        return self._client.current_user_id() if self.available else None

    def has(self, key: str) -> bool:
        # Fails closed: no session or any error means "not entitled".
        return self.available and bool(self._client.check_entitlement(key))

    def license_model(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise EntitlementUnavailable(
                f"{model_key} is a licensed model: sign in to Team Tech Tools "
                "from the dashboard"
            )
        return self._client.acquire_model_license(
            model_key, channel=channel, pipeline_version=pipeline_version
        )

    def list_plugins(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        return list(self._client.get_available_plugins() or [])

    def fetch_plugin(self, key: str, dest: Path) -> None:
        if not self.available:
            raise EntitlementUnavailable(
                f"plugin {key}: sign in to Team Tech Tools from the dashboard"
            )
        self._client.download_plugin(key, dest)
