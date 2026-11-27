"""In-memory IEntitlements for tests and offline runs. Not registered: it is
never picked for a real install, only handed in explicitly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import EntitlementUnavailable, IEntitlements


class FakeEntitlements(IEntitlements):
    name = "fake"

    def __init__(
        self,
        licensee: str | None = "user-1",
        keys: set[str] | None = None,
        licenses: dict[str, dict[str, Any]] | None = None,
        plugins: dict[str, bytes] | None = None,
        plugin_listing: list[dict[str, Any]] | None = None,
    ):
        self._licensee = licensee
        self.keys = set(keys or ())
        self.licenses = dict(licenses or {})
        self.plugins = dict(plugins or {})
        self.plugin_listing = plugin_listing
        self.calls: list[tuple] = []

    @classmethod
    def from_context(cls, context) -> None:
        return None

    @property
    def available(self) -> bool:
        return self._licensee is not None

    def licensee(self) -> str | None:
        return self._licensee

    def has(self, key: str) -> bool:
        self.calls.append(("has", key))
        return self.available and key in self.keys

    def license_model(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("license_model", model_key, channel, pipeline_version))
        if not self.available or model_key not in self.licenses:
            raise EntitlementUnavailable(f"no licence for {model_key}")
        return self.licenses[model_key]

    def list_plugins(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        if self.plugin_listing is not None:
            return list(self.plugin_listing)
        return [{"key": k} for k in self.plugins]

    def fetch_plugin(self, key: str, dest: Path) -> None:
        self.calls.append(("fetch_plugin", key))
        if not self.available or key not in self.plugins:
            raise EntitlementUnavailable(f"no plugin {key}")
        Path(dest).write_bytes(self.plugins[key])
