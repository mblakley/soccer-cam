"""Tests for video_grouper.ball_tracking.secure_loader."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Optional
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from video_grouper.ball_tracking import secure_loader as sl
from video_grouper.ball_tracking.secure_loader import (
    SecureLoader,
    SecureLoaderError,
)


# Disable root-level autouse fixtures.
@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_httpx():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "11111111-1111-1111-1111-111111111111"
MODEL_KEY = "ball_detection"
VERSION = "1.0.0"
MASTER_KEY_ID = "mk-test"


def _gen_keypair() -> tuple[Ed25519PrivateKey, str]:
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


def _build_artifact(plaintext: bytes, content_key: bytes) -> bytes:
    nonce = b"\x00" * 12  # deterministic for tests
    header = {
        "model_key": MODEL_KEY,
        "version": VERSION,
        "master_key_id": MASTER_KEY_ID,
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "format_version": 1,
    }
    header_bytes = sl._canonical_json(header)
    aad = sl._build_aad(MODEL_KEY, VERSION, MASTER_KEY_ID)
    ct = AESGCM(content_key).encrypt(nonce, plaintext, aad)
    out = bytearray()
    out += sl.ARTIFACT_MAGIC
    out += bytes([sl.ARTIFACT_FORMAT_VERSION])
    out += len(header_bytes).to_bytes(4, "big")
    out += header_bytes
    out += ct
    return bytes(out)


def _build_license_response(
    priv: Ed25519PrivateKey,
    artifact_bytes: bytes,
    wrapped_key: bytes,
    *,
    user_id: str = USER_ID,
    expires_at: Optional[datetime] = None,
    artifact_url: str = "https://cdn.example/balldet-1.0.0.enc",
    tier: str = "free",
    version: str = VERSION,
) -> dict:
    expires = expires_at or datetime.now(UTC) + timedelta(days=30)
    manifest = {
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "artifact_url": artifact_url,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "license_id": "abcd-1234",
        "master_key_id": MASTER_KEY_ID,
        "model_key": MODEL_KEY,
        "model_version": version,
        "tier": tier,
        "user_id": user_id,
    }
    manifest_bytes = sl._canonical_json(manifest)
    sig = priv.sign(manifest_bytes)
    return {
        "license_id": manifest["license_id"],
        "license_token": base64.b64encode(manifest_bytes).decode("ascii"),
        "license_signature": sig.hex(),
        "wrapped_key": wrapped_key.hex(),
        "model_key": MODEL_KEY,
        "version": version,
        "tier": tier,
        "expires_at": manifest["expires_at"],
    }


def _make_ttt_client(license_response: dict, user_id: str = USER_ID):
    """Mock TTT client that returns the prepared license."""
    ttt = MagicMock()
    ttt.is_authenticated.return_value = True
    ttt.current_user_id.return_value = user_id
    ttt.acquire_model_license.return_value = license_response
    return ttt


def _make_http_client(artifact_bytes: bytes):
    """Mock httpx client that returns the artifact bytes on GET."""
    http = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.content = artifact_bytes
    http.get.return_value = response
    return http


def _fake_session_factory(monkeypatch, providers=("CPUExecutionProvider",)):
    """Replace onnxruntime.InferenceSession with a stub so we don't need a real model.

    Patches the lazy `_onnxruntime()` accessor; works even when the real
    onnxruntime package fails to load (e.g. missing DirectX runtime).
    """
    fake_session = MagicMock()
    fake_session.get_providers.return_value = [providers[0]]

    fake_session_class = MagicMock(return_value=fake_session)

    fake_module = MagicMock()
    fake_module.InferenceSession = fake_session_class
    fake_module.get_available_providers = lambda: list(providers)

    monkeypatch.setattr(sl, "_onnxruntime", lambda: fake_module)
    return fake_session_class


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAcquireHappyPath:
    def test_loads_session_for_entitled_user(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        plaintext = b"FAKE_ONNX_BYTES_FOR_TESTING"
        artifact = _build_artifact(plaintext, content_key)
        license_response = _build_license_response(priv, artifact, content_key)

        fake_session_class = _fake_session_factory(monkeypatch)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        loaded = loader.acquire(MODEL_KEY)

        assert loaded.model_key == MODEL_KEY
        assert loaded.version == VERSION
        assert loaded.tier == "free"
        assert loaded.provider == "CPUExecutionProvider"
        # ONNX session was constructed with decrypted bytes
        fake_session_class.assert_called_once()
        args, kwargs = fake_session_class.call_args
        assert args[0] == plaintext

    def test_passes_channel_through_to_ttt(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x33" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(priv, artifact, content_key)
        _fake_session_factory(monkeypatch)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        loader.acquire(MODEL_KEY, channel="beta", pipeline_version="2.0.0")

        ttt.acquire_model_license.assert_called_once_with(
            MODEL_KEY, channel="beta", pipeline_version="2.0.0"
        )


# ---------------------------------------------------------------------------
# License verification failures
# ---------------------------------------------------------------------------


class TestLicenseVerification:
    def test_rejects_unsigned_license(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        wrong_priv, _ = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        # Sign with the WRONG key
        license_response = _build_license_response(wrong_priv, artifact, content_key)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="signature"):
            loader.acquire(MODEL_KEY)

    def test_rejects_expired_license(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(
            priv,
            artifact,
            content_key,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="expired"):
            loader.acquire(MODEL_KEY)

    def test_rejects_license_for_different_user(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        # License is for OTHER_USER_ID
        license_response = _build_license_response(
            priv, artifact, content_key, user_id=OTHER_USER_ID
        )

        # But the authenticated user is USER_ID
        ttt = _make_ttt_client(license_response, user_id=USER_ID)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="user_id"):
            loader.acquire(MODEL_KEY)

    def test_accepts_any_of_multiple_public_keys(self, monkeypatch):
        # Old key still listed alongside the new one — rotation-friendly.
        old_priv, old_pub = _gen_keypair()
        new_priv, new_pub = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)

        # Server is now signing with the NEW key; client lists both.
        license_response = _build_license_response(new_priv, artifact, content_key)
        _fake_session_factory(monkeypatch)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [old_pub, new_pub], http_client=http)

        loader.acquire(MODEL_KEY)  # no exception

        # And vice versa — server still on old key
        license_response_old = _build_license_response(old_priv, artifact, content_key)
        ttt2 = _make_ttt_client(license_response_old)
        http2 = _make_http_client(artifact)
        loader2 = SecureLoader(ttt2, [old_pub, new_pub], http_client=http2)
        loader2.acquire(MODEL_KEY)


# ---------------------------------------------------------------------------
# Artifact tampering
# ---------------------------------------------------------------------------


class TestArtifactTampering:
    def test_rejects_artifact_with_tampered_version_in_header(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(priv, artifact, content_key)

        # Flip "1.0.0" to "9.9.9" in the artifact header (same length)
        tampered = artifact.replace(b'"version":"1.0.0"', b'"version":"9.9.9"', 1)
        # Need to update the SHA in the manifest to point at the tampered bytes
        # so we exercise the AAD check, not the SHA check first.
        license_response["wrapped_key"]  # noqa
        license_for_tampered = _build_license_response(priv, tampered, content_key)
        # But the manifest now claims version=9.9.9 which won't match the artifact
        # header anyway, so the loader catches one or the other.

        ttt = _make_ttt_client(license_for_tampered)
        http = _make_http_client(tampered)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError):
            loader.acquire(MODEL_KEY)

    def test_rejects_when_artifact_sha_does_not_match_manifest(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(priv, artifact, content_key)

        # Hand the loader DIFFERENT bytes than what the manifest's SHA claims
        different_artifact = _build_artifact(b"y" * 100, content_key)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(different_artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="SHA-256"):
            loader.acquire(MODEL_KEY)

    def test_rejects_artifact_with_bad_magic(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)

        bad_artifact = b"WRNG" + artifact[4:]
        # SHA in manifest will mismatch — but also the magic check fires; either
        # error is acceptable. We assert the loader rejects.
        license_for_bad = _build_license_response(priv, bad_artifact, content_key)

        ttt = _make_ttt_client(license_for_bad)
        http = _make_http_client(bad_artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="magic"):
            loader.acquire(MODEL_KEY)

    def test_rejects_artifact_with_wrong_wrapped_key(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        # Build the license but rewrite the wrapped_key to be wrong
        wrong_key = b"\x77" * 32
        license_response = _build_license_response(priv, artifact, wrong_key)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="tag"):
            loader.acquire(MODEL_KEY)


# ---------------------------------------------------------------------------
# Auth + transport
# ---------------------------------------------------------------------------


class TestAuthAndTransport:
    def test_unauthenticated_client_raises(self, monkeypatch):
        ttt = MagicMock()
        ttt.is_authenticated.return_value = False
        loader = SecureLoader(ttt, ["abcd"], http_client=MagicMock())

        with pytest.raises(SecureLoaderError, match="authenticated"):
            loader.acquire(MODEL_KEY)

    def test_ttt_error_propagates_as_loader_error(self, monkeypatch):
        ttt = MagicMock()
        ttt.is_authenticated.return_value = True
        ttt.current_user_id.return_value = USER_ID
        ttt.acquire_model_license.side_effect = RuntimeError("403 not entitled")

        loader = SecureLoader(ttt, ["abcd"], http_client=MagicMock())

        with pytest.raises(SecureLoaderError, match="Could not acquire license"):
            loader.acquire(MODEL_KEY)

    def test_artifact_download_failure_raises(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(priv, artifact, content_key)

        http = MagicMock()
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        http.get.return_value = bad_resp

        ttt = _make_ttt_client(license_response)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        with pytest.raises(SecureLoaderError, match="HTTP 500"):
            loader.acquire(MODEL_KEY)


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def _patch_providers(monkeypatch, providers: list[str]) -> None:
    fake_module = MagicMock()
    fake_module.get_available_providers = lambda: providers
    monkeypatch.setattr(sl, "_onnxruntime", lambda: fake_module)


class TestProviderSelection:
    def test_prefers_cuda_when_available(self, monkeypatch):
        _patch_providers(
            monkeypatch,
            ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
        )
        assert sl._select_providers()[0] == "CUDAExecutionProvider"

    def test_falls_back_to_directml_when_no_cuda(self, monkeypatch):
        _patch_providers(monkeypatch, ["DmlExecutionProvider", "CPUExecutionProvider"])
        assert sl._select_providers()[0] == "DmlExecutionProvider"

    def test_falls_back_to_cpu_when_nothing_else(self, monkeypatch):
        _patch_providers(monkeypatch, ["CPUExecutionProvider"])
        assert sl._select_providers() == ["CPUExecutionProvider"]

    def test_returns_cpu_when_provider_list_is_empty(self, monkeypatch):
        _patch_providers(monkeypatch, [])
        assert sl._select_providers() == ["CPUExecutionProvider"]


# ---------------------------------------------------------------------------
# Tier observability
# ---------------------------------------------------------------------------


class TestTier:
    def test_premium_tier_surfaced_in_loaded_model(self, monkeypatch):
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(
            priv, artifact, content_key, tier="premium"
        )
        _fake_session_factory(monkeypatch)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        loaded = loader.acquire(MODEL_KEY)
        assert loaded.tier == "premium"

    def test_free_tier_surfaced_when_lapsed_subscriber_downshifts(self, monkeypatch):
        # Same loader; server returns a free-tier license this time.
        priv, pub_hex = _gen_keypair()
        content_key = b"\x42" * 32
        artifact = _build_artifact(b"x", content_key)
        license_response = _build_license_response(
            priv, artifact, content_key, tier="free"
        )
        _fake_session_factory(monkeypatch)

        ttt = _make_ttt_client(license_response)
        http = _make_http_client(artifact)
        loader = SecureLoader(ttt, [pub_hex], http_client=http)

        loaded = loader.acquire(MODEL_KEY)
        # Caller (tray UI etc.) can compare against the user's last-known tier
        # to decide whether to surface a "now using free" notification.
        assert loaded.tier == "free"
