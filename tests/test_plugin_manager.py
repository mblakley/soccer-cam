"""Tests for PluginManager: sync, verification gating, refresh-on-headroom, load."""

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from video_grouper.plugins.plugin_manager import PluginManager


@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_httpx():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


USER_ID = "11111111-1111-1111-1111-111111111111"


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return priv, pub_hex


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _make_signed_zip(
    zip_path: Path,
    priv: Ed25519PrivateKey,
    *,
    version: str = "1.0.0",
    user_id: str = USER_ID,
    expires_in_days: int = 30,
) -> None:
    plugin_files = {
        "plugin/__init__.py": b"VALUE = 42\n",
    }
    files_meta = [
        {"path": p, "sha256": hashlib.sha256(c).hexdigest()}
        for p, c in plugin_files.items()
    ]
    expires = datetime.now(UTC) + timedelta(days=expires_in_days)
    manifest = {
        "plugin_key": "premium.test.feature",
        "version": version,
        "user_id": user_id,
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files_meta,
    }
    manifest_bytes = _canonical(manifest)
    signature = priv.sign(manifest_bytes)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("manifest.json.sig", signature.hex())
        for p, c in plugin_files.items():
            zf.writestr(p, c)


def _make_ttt_client(priv: Ed25519PrivateKey, *, available: list[dict]) -> MagicMock:
    client = MagicMock()
    client.current_user_id = MagicMock(return_value=USER_ID)
    client.get_available_plugins = MagicMock(return_value=available)

    def fake_download(key: str, dest_path: Path) -> None:
        _make_signed_zip(dest_path, priv, version=_version_for(available, key))

    client.download_plugin = MagicMock(side_effect=fake_download)
    return client


def _version_for(available: list[dict], key: str) -> str:
    for entry in available:
        if entry["key"] == key:
            return entry.get("version", "1.0.0")
    return "1.0.0"


# ---------------------------------------------------------------------------
# sync + install
# ---------------------------------------------------------------------------


class TestSyncInstall:
    def test_downloads_and_installs_new_plugin(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()

        plugin_dir = tmp_path / "plugins" / "premium.test.feature"
        assert plugin_dir.is_dir()
        assert (plugin_dir / "manifest.json").is_file()
        assert (plugin_dir / "plugin" / "__init__.py").is_file()

    def test_skips_when_already_up_to_date(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()
        assert client.download_plugin.call_count == 1

        # Second sync with same version + fresh manifest — no re-download
        client.download_plugin.reset_mock()
        mgr.sync_plugins()
        assert client.download_plugin.call_count == 0

    def test_refresh_on_expiry_headroom(self, tmp_path):
        priv, pub = _keypair()
        # Install a plugin manually with a manifest expiring in 5 days
        plugin_dir = tmp_path / "plugins" / "premium.test.feature"
        plugin_dir.mkdir(parents=True)
        staging_zip = tmp_path / "staging.zip"
        _make_signed_zip(staging_zip, priv, version="1.0.0", expires_in_days=5)
        with zipfile.ZipFile(staging_zip) as zf:
            zf.extractall(plugin_dir)

        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        # 7-day headroom on a 5-day manifest → refresh triggers
        mgr = PluginManager(
            client, tmp_path, public_keys=[pub], refresh_headroom_days=7
        )
        mgr.sync_plugins()
        assert client.download_plugin.call_count == 1

    def test_bad_signature_refuses_install(self, tmp_path):
        priv, _ = _keypair()
        _, wrong_pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[wrong_pub])
        mgr.sync_plugins()
        assert not (tmp_path / "plugins" / "premium.test.feature").exists()

    def test_removes_plugin_no_longer_available(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()
        assert (tmp_path / "plugins" / "premium.test.feature").is_dir()

        client.get_available_plugins.return_value = []
        mgr.sync_plugins()
        assert not (tmp_path / "plugins" / "premium.test.feature").exists()

    def test_no_current_user_refuses_install(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        client.current_user_id = MagicMock(return_value=None)
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()
        assert not (tmp_path / "plugins" / "premium.test.feature").exists()


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


class TestLoad:
    def test_loads_valid_plugin(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()
        mgr.load_plugins()
        assert "premium.test.feature" in mgr.get_loaded_plugins()

    def test_skips_tampered_plugin(self, tmp_path):
        priv, pub = _keypair()
        client = _make_ttt_client(
            priv,
            available=[
                {"key": "premium.test.feature", "version": "1.0.0"},
            ],
        )
        mgr = PluginManager(client, tmp_path, public_keys=[pub])
        mgr.sync_plugins()

        # Tamper with the extracted file after install
        tampered = (
            tmp_path / "plugins" / "premium.test.feature" / "plugin" / "__init__.py"
        )
        tampered.write_bytes(b"VALUE = 999\n")

        mgr.load_plugins()
        assert "premium.test.feature" not in mgr.get_loaded_plugins()
