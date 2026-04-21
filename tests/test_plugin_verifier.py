"""Tests for plugin signature verification."""

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from video_grouper.plugins.plugin_verifier import (
    read_manifest_expires_at,
    verify_extracted_plugin,
    verify_plugin_bundle,
)


# Disable root-level autouse fixtures that patch filesystem/httpx for these tests.
@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_httpx():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


USER_ID = "00000000-0000-0000-0000-000000000001"


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def _build_bundle(
    tmp_path: Path,
    priv: Ed25519PrivateKey,
    *,
    user_id: str = USER_ID,
    expires_at: datetime | None = None,
    plugin_files: dict[str, bytes] | None = None,
    tamper_file: str | None = None,
    drop_file: str | None = None,
    override_user_id_in_sig: bool = False,
) -> Path:
    plugin_files = plugin_files or {
        "plugin/__init__.py": b"from . import mod\n",
        "plugin/mod.py": b"VALUE = 1\n",
    }
    files_meta = []
    for path, content in plugin_files.items():
        files_meta.append({"path": path, "sha256": hashlib.sha256(content).hexdigest()})

    expires = expires_at or datetime.now(UTC) + timedelta(days=30)
    manifest = {
        "plugin_key": "premium.test.feature",
        "version": "1.0.0",
        "user_id": user_id,
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files_meta,
    }
    manifest_bytes = _canonical(manifest)
    signature = priv.sign(manifest_bytes)

    # If we override user_id after signing, the stored manifest won't match the sig
    if override_user_id_in_sig:
        manifest["user_id"] = "99999999-9999-9999-9999-999999999999"
        manifest_bytes = _canonical(manifest)

    zip_path = tmp_path / "plugin.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("manifest.json.sig", signature.hex())
        for path, content in plugin_files.items():
            if path == drop_file:
                continue
            if path == tamper_file:
                content = content + b"tamper"
            zf.writestr(path, content)
    return zip_path


# ---------------------------------------------------------------------------
# verify_plugin_bundle
# ---------------------------------------------------------------------------


class TestVerifyPluginBundle:
    def test_happy_path(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is True

    def test_signature_mismatch_rejected(self, tmp_path):
        priv, _ = _keypair()
        _, other_pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        assert verify_plugin_bundle(zip_path, [other_pub], USER_ID) is False

    def test_user_id_mismatch_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv, user_id=USER_ID)
        assert verify_plugin_bundle(zip_path, [pub], "different-user-id") is False

    def test_manifest_expired_rejected(self, tmp_path):
        priv, pub = _keypair()
        past = datetime.now(UTC) - timedelta(days=1)
        zip_path = _build_bundle(tmp_path, priv, expires_at=past)
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is False

    def test_file_hash_mismatch_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv, tamper_file="plugin/mod.py")
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is False

    def test_missing_file_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv, drop_file="plugin/mod.py")
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is False

    def test_missing_manifest_rejected(self, tmp_path):
        zip_path = tmp_path / "no-manifest.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("plugin/__init__.py", b"")
        _, pub = _keypair()
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is False

    def test_multi_key_first_match_accepted(self, tmp_path):
        """Multi-key list supports rotation: verify passes if any listed key matches."""
        priv_old, pub_old = _keypair()
        _, pub_new = _keypair()
        zip_path = _build_bundle(tmp_path, priv_old)
        assert verify_plugin_bundle(zip_path, [pub_new, pub_old], USER_ID) is True

    def test_empty_public_key_list_rejected(self, tmp_path):
        priv, _ = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        assert verify_plugin_bundle(zip_path, [], USER_ID) is False

    def test_tampered_manifest_after_signing_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv, override_user_id_in_sig=True)
        assert verify_plugin_bundle(zip_path, [pub], USER_ID) is False

    def test_not_a_zip_rejected(self, tmp_path):
        fake = tmp_path / "not.zip"
        fake.write_bytes(b"not a zip")
        _, pub = _keypair()
        assert verify_plugin_bundle(fake, [pub], USER_ID) is False


# ---------------------------------------------------------------------------
# verify_extracted_plugin
# ---------------------------------------------------------------------------


class TestVerifyExtractedPlugin:
    def _extract(self, zip_path: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)

    def test_happy_path(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        extracted = tmp_path / "extracted"
        self._extract(zip_path, extracted)
        assert verify_extracted_plugin(extracted, [pub], USER_ID) is True

    def test_tampered_file_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        extracted = tmp_path / "extracted"
        self._extract(zip_path, extracted)
        # Tamper with the extracted file
        (extracted / "plugin" / "mod.py").write_bytes(b"tampered")
        assert verify_extracted_plugin(extracted, [pub], USER_ID) is False

    def test_expired_rejected(self, tmp_path):
        priv, pub = _keypair()
        past = datetime.now(UTC) - timedelta(days=1)
        zip_path = _build_bundle(tmp_path, priv, expires_at=past)
        extracted = tmp_path / "extracted"
        self._extract(zip_path, extracted)
        assert verify_extracted_plugin(extracted, [pub], USER_ID) is False

    def test_user_id_mismatch_rejected(self, tmp_path):
        priv, pub = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        extracted = tmp_path / "extracted"
        self._extract(zip_path, extracted)
        assert verify_extracted_plugin(extracted, [pub], "other-user") is False


# ---------------------------------------------------------------------------
# read_manifest_expires_at
# ---------------------------------------------------------------------------


class TestReadManifestExpiresAt:
    def test_reads_valid_manifest(self, tmp_path):
        priv, _ = _keypair()
        zip_path = _build_bundle(tmp_path, priv)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
        result = read_manifest_expires_at(extracted / "manifest.json")
        assert result is not None
        assert result.tzinfo is not None

    def test_missing_returns_none(self, tmp_path):
        assert read_manifest_expires_at(tmp_path / "nope.json") is None
