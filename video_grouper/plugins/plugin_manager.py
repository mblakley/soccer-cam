"""
Plugin lifecycle manager: check entitlements, download, verify, cache, load.

Manages premium plugin packages downloaded from TTT API.
"""

import importlib
import json
import logging
import shutil
import sys
import zipfile
from pathlib import Path

from .plugin_verifier import verify_plugin_signature

logger = logging.getLogger(__name__)


class PluginManager:
    """Manages the full plugin lifecycle: check entitlements, download, verify, cache, load."""

    def __init__(self, ttt_client, storage_path: Path, signing_key: str):
        self.ttt_client = ttt_client
        self.plugins_dir = storage_path / "plugins"
        self.signing_key = signing_key
        self._loaded_plugins: dict = {}

    def sync_plugins(self) -> None:
        """
        Sync plugins with TTT API:
        1. Fetch available plugins (entitlement-filtered)
        2. Download new/updated versions
        3. Remove plugins no longer entitled
        """
        try:
            available = self.ttt_client.get_available_plugins()
        except Exception:
            logger.warning("Failed to fetch available plugins from TTT", exc_info=True)
            return

        available_keys = {p["key"] for p in available}

        # Download new/updated plugins
        for plugin_info in available:
            key = plugin_info["key"]
            version = plugin_info.get("version", "0.0.0")
            local_version = self._get_local_version(key)

            if local_version == version:
                logger.debug("Plugin %s is up to date (v%s)", key, version)
                continue

            logger.info(
                "Downloading plugin %s v%s (local: %s)",
                key,
                version,
                local_version or "none",
            )
            self._download_and_install(key)

        # Remove plugins no longer entitled
        if self.plugins_dir.exists():
            for plugin_dir in self.plugins_dir.iterdir():
                if plugin_dir.is_dir() and plugin_dir.name not in available_keys:
                    logger.info(
                        "Removing plugin %s (no longer entitled)", plugin_dir.name
                    )
                    shutil.rmtree(plugin_dir, ignore_errors=True)

    def _get_local_version(self, key: str) -> str | None:
        """Get the locally cached version of a plugin, or None if not cached."""
        manifest_path = self.plugins_dir / key / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path) as f:
                return json.load(f).get("version")
        except Exception:
            return None

    def _download_and_install(self, key: str) -> bool:
        """Download a plugin zip from TTT, verify signature, extract to cache."""
        zip_path = self.plugins_dir / f"{key}.zip"
        plugin_dir = self.plugins_dir / key

        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            self.ttt_client.download_plugin(key, zip_path)

            # Verify signature
            if self.signing_key and not verify_plugin_signature(
                zip_path, self.signing_key
            ):
                logger.error("Plugin %s failed signature verification — skipping", key)
                zip_path.unlink(missing_ok=True)
                return False

            # Extract
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
            plugin_dir.mkdir(parents=True)

            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(plugin_dir)

            # Clean up zip
            zip_path.unlink(missing_ok=True)

            logger.info("Plugin %s installed successfully", key)
            return True

        except Exception:
            logger.exception("Failed to download/install plugin %s", key)
            zip_path.unlink(missing_ok=True)
            return False

    def load_plugins(self) -> None:
        """Load all cached plugin modules via importlib."""
        if not self.plugins_dir.exists():
            return

        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            plugin_module_dir = plugin_dir / "plugin"
            init_file = plugin_module_dir / "__init__.py"

            if not init_file.exists():
                logger.debug(
                    "Plugin %s has no plugin/__init__.py — skipping", plugin_dir.name
                )
                continue

            try:
                # Add plugin directory to sys.path temporarily
                str_path = str(plugin_module_dir.parent)
                if str_path not in sys.path:
                    sys.path.insert(0, str_path)

                module = importlib.import_module("plugin")
                self._loaded_plugins[plugin_dir.name] = module
                logger.info("Loaded plugin: %s", plugin_dir.name)

                # Remove from sys.path and sys.modules to avoid conflicts
                if str_path in sys.path:
                    sys.path.remove(str_path)
                if "plugin" in sys.modules:
                    del sys.modules["plugin"]

            except Exception:
                logger.exception("Failed to load plugin %s", plugin_dir.name)

    def get_loaded_plugins(self) -> dict:
        """Return dict of loaded plugin modules keyed by plugin name."""
        return dict(self._loaded_plugins)

    def check_entitlements(self) -> None:
        """Re-check entitlements and disable plugins if no longer entitled."""
        try:
            available = self.ttt_client.get_available_plugins()
            available_keys = {p["key"] for p in available}

            # Unload plugins no longer entitled
            for key in list(self._loaded_plugins.keys()):
                if key not in available_keys:
                    logger.info("Plugin %s no longer entitled — unloading", key)
                    del self._loaded_plugins[key]

        except Exception:
            logger.warning(
                "Failed to check entitlements — keeping current state", exc_info=True
            )
