"""No entitlements provider (TTT off): nothing is granted, and asking for a
licensed model says how to fix it. Community / bring-your-own models
(``model_path``) never come here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from video_grouper.integrations.registry import NULL_PRIORITY, provides

from ..base import EntitlementUnavailable, IEntitlements

_HOW = (
    "sign in to Team Tech Tools from the dashboard, or use model_path for a "
    "community / bring-your-own model"
)


@provides(IEntitlements)
class NullEntitlements(IEntitlements):
    name = "none"
    priority = NULL_PRIORITY

    @classmethod
    def from_context(cls, context) -> NullEntitlements:
        return cls()

    @property
    def available(self) -> bool:
        return False

    def licensee(self) -> str | None:
        return None

    def has(self, key: str) -> bool:
        return False

    def license_model(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> dict[str, Any]:
        raise EntitlementUnavailable(f"{model_key} is a licensed model: {_HOW}")

    def list_plugins(self) -> list[dict[str, Any]]:
        raise EntitlementUnavailable(f"plugins need an entitlement: {_HOW}")

    def fetch_plugin(self, key: str, dest: Path) -> None:
        raise EntitlementUnavailable(f"plugin {key} needs an entitlement: {_HOW}")
