"""Plugin lifecycle manager: check availability, download, verify, cache, load.

Downloads per-user signed bundles from TTT, verifies the Ed25519 signature on
the manifest, extracts into a per-plugin cache directory, and imports the
plugin module via importlib. Re-verifies at load time so expired or tampered
manifests are refused even after the original download passed.
"""

import importlib
import json
import logging
import shutil
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .plugin_verifier import (
    read_manifest_expires_at,
    verify_extracted_plugin,
    verify_plugin_bundle,
)

logger = logging.getLogger(__name__)


class PluginManager:
    """Manages the full plugin lifecycle: fetch, verify, cache, load."""

    def __init__(
        self,
        ttt_client,
        storage_path: Path,
        public_keys: list[str],
        refresh_headroom_days: int = 7,
    ):
        self.ttt_client = ttt_client
        self.plugins_dir = storage_path / "plugins"
        self.public_keys = list(public_keys)
        self.refresh_headroom = timedelta(days=refresh_headroom_days)
        self._loaded_plugins: dict = {}

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_plugins(self) -> None:
        """Fetch available plugins, download new or soon-to-expire ones, drop stale ones."""
        try:
            available = self.ttt_client.get_available_plugins()
        except Exception:
            logger.warning("Failed to fetch available plugins from TTT", exc_info=True)
            return

        available_keys = {p["key"] for p in available}

        for plugin_info in available:
            key = plugin_info["key"]
            version = plugin_info.get("version", "0.0.0")
            local_version = self._get_local_version(key)
            needs_refresh = self._manifest_needs_refresh(key)

            if local_version == version and not needs_refresh:
                logger.debug("Plugin %s is up to date (v%s)", key, version)
                continue

            reason = (
                f"version {local_version or 'none'} -> {version}"
                if local_version != version
                else "manifest approaching expiry"
            )
            logger.info("Refreshing plugin %s (%s)", key, reason)
            self._download_and_install(key)

        # Remove plugins no longer listed
        if self.plugins_dir.exists():
            for plugin_dir in self.plugins_dir.iterdir():
                if plugin_dir.is_dir() and plugin_dir.name not in available_keys:
                    logger.info(
                        "Removing plugin %s (no longer listed)", plugin_dir.name
                    )
                    shutil.rmtree(plugin_dir, ignore_errors=True)

    def _get_local_version(self, key: str) -> str | None:
        manifest_path = self.plugins_dir / key / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_bytes()).get("version")
        except Exception:
            return None

    def _manifest_needs_refresh(self, key: str) -> bool:
        manifest_path = self.plugins_dir / key / "manifest.json"
        expires_at = read_manifest_expires_at(manifest_path)
        if expires_at is None:
            # No manifest or unreadable — treat as needing download
            return True
        return (expires_at - datetime.now(UTC)) < self.refresh_headroom

    # ------------------------------------------------------------------
    # Download + verify + extract
    # ------------------------------------------------------------------

    def _download_and_install(self, key: str) -> bool:
        zip_path = self.plugins_dir / f"{key}.zip"
        plugin_dir = self.plugins_dir / key

        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            self.ttt_client.download_plugin(key, zip_path)

            user_id = self.ttt_client.current_user_id
            if not user_id:
                logger.warning("Cannot install plugin %s: no authenticated user", key)
                zip_path.unlink(missing_ok=True)
                return False

            if not verify_plugin_bundle(zip_path, self.public_keys, user_id):
                logger.warning("Plugin %s verification failed, not installing", key)
                zip_path.unlink(missing_ok=True)
                return False

            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
            plugin_dir.mkdir(parents=True)

            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(plugin_dir)

            zip_path.unlink(missing_ok=True)
            logger.info("Plugin %s installed", key)
            return True

        except Exception:
            logger.exception("Failed to download/install plugin %s", key)
            zip_path.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_plugins(self) -> None:
        """Load all cached plugin modules after re-verifying them on disk."""
        if not self.plugins_dir.exists():
            return

        user_id = self.ttt_client.current_user_id
        if not user_id:
            logger.warning("Cannot load plugins: no authenticated user")
            return

        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            if not verify_extracted_plugin(plugin_dir, self.public_keys, user_id):
                logger.warning(
                    "Plugin %s did not pass on-disk verification, skipping",
                    plugin_dir.name,
                )
                continue

            plugin_module_dir = plugin_dir / "plugin"
            init_file = plugin_module_dir / "__init__.py"
            if not init_file.exists():
                logger.debug(
                    "Plugin %s has no plugin/__init__.py, skipping", plugin_dir.name
                )
                continue

            try:
                str_path = str(plugin_module_dir.parent)
                if str_path not in sys.path:
                    sys.path.insert(0, str_path)
                module = importlib.import_module("plugin")
                self._loaded_plugins[plugin_dir.name] = module
                logger.info("Loaded plugin: %s", plugin_dir.name)

                if str_path in sys.path:
                    sys.path.remove(str_path)
                if "plugin" in sys.modules:
                    del sys.modules["plugin"]
            except Exception:
                logger.exception("Failed to load plugin %s", plugin_dir.name)

    def get_loaded_plugins(self) -> dict:
        return dict(self._loaded_plugins)

    def check_entitlements(self) -> None:
        """Re-check available plugins and unload any no longer listed."""
        try:
            available = self.ttt_client.get_available_plugins()
            available_keys = {p["key"] for p in available}
            for key in list(self._loaded_plugins.keys()):
                if key not in available_keys:
                    logger.info("Plugin %s no longer listed, unloading", key)
                    del self._loaded_plugins[key]
        except Exception:
            logger.warning(
                "Failed to check entitlements, keeping current state", exc_info=True
            )
