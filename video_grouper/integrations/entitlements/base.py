"""IEntitlements: what this account may use (premium features, licensed models,
plugins), and the account id those grants are bound to."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from video_grouper.integrations.registry import Provider


class EntitlementUnavailable(RuntimeError):
    """No provider can grant this right now (not signed in, or no provider)."""


class IEntitlements(Provider, ABC):
    @property
    @abstractmethod
    def available(self) -> bool:
        """True when a provider is signed in and can grant things."""

    @abstractmethod
    def licensee(self) -> str | None:
        """The account id licences and plugin bundles are bound to (None when
        no account is held). Does not require ``available``: installed bundles
        stay usable offline until their own signed expiry."""

    @abstractmethod
    def has(self, key: str) -> bool:
        """Whether the account holds entitlement ``key``. False when unsure."""

    @abstractmethod
    def license_model(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> dict[str, Any]:
        """A signed licence for ``model_key`` (verified by the caller against
        soccer-cam's own trust anchors). Raises EntitlementUnavailable."""

    @abstractmethod
    def list_plugins(self) -> list[dict[str, Any]]:
        """Plugins this account may install. Raises EntitlementUnavailable when
        it can't say (not signed in, no provider): ``[]`` means "entitled to
        none", and installed plugins that aren't listed are removed."""

    @abstractmethod
    def fetch_plugin(self, key: str, dest: Path) -> None:
        """Download plugin ``key``'s signed bundle to ``dest``."""
