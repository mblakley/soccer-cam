"""Acquire, decrypt, and load a ball-detection ONNX session from TTT."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

ARTIFACT_MAGIC = b"TTBM"
ARTIFACT_FORMAT_VERSION = 1


def _onnxruntime():
    """Lazy import of onnxruntime so the module is importable even if the
    runtime fails to load (e.g. missing DirectX runtime in CI / unit tests)."""
    return importlib.import_module("onnxruntime")


# Native (Cython-compiled) implementations are loaded when available.
# Production builds compile _secure_loader_native.pyx to .pyd/.so/.dylib;
# dev/test workflows fall back to the pure-Python definitions below.
try:
    from video_grouper.ball_tracking import _secure_loader_native as _native

    _NATIVE_AVAILABLE = True
    logger.debug("secure_loader: using native module")
except ImportError:
    _native = None
    _NATIVE_AVAILABLE = False
    logger.debug("secure_loader: native module not built; using pure-Python fallback")


class SecureLoaderError(Exception):
    """Raised when a model cannot be loaded for any reason."""


@dataclass
class LoadedModel:
    session: Any
    model_key: str
    version: str
    tier: str
    provider: str


def _translate_native_error(exc: Exception) -> SecureLoaderError:
    """Re-raise native module's NativeSecureLoaderError as SecureLoaderError."""
    return SecureLoaderError(str(exc))


def _canonical_json(obj: dict) -> bytes:
    if _NATIVE_AVAILABLE:
        return _native.canonical_json(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_expires_at(value: str) -> datetime:
    if _NATIVE_AVAILABLE:
        return _native.parse_expires_at(value)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _verify_signature(
    message: bytes, signature_hex: str, public_keys: list[str]
) -> bool:
    if _NATIVE_AVAILABLE:
        return _native.verify_signature(message, signature_hex, public_keys)
    try:
        sig = bytes.fromhex(signature_hex.strip())
    except ValueError:
        return False
    for key_hex in public_keys:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
            pub.verify(sig, message)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def _verify_license(
    license_response: dict,
    public_keys: list[str],
    expected_user_id: str,
    now: Optional[datetime] = None,
) -> dict:
    """Validate the license response shape and signature; return the parsed manifest."""
    if _NATIVE_AVAILABLE:
        try:
            return _native.verify_license(
                license_response, public_keys, expected_user_id, now
            )
        except _native.NativeSecureLoaderError as exc:
            raise _translate_native_error(exc) from exc

    check_time = now or datetime.now(UTC)
    try:
        token_b64 = license_response["license_token"]
        signature_hex = license_response["license_signature"]
    except KeyError as exc:
        raise SecureLoaderError(f"License response missing field: {exc}") from None

    try:
        manifest_bytes = base64.b64decode(token_b64)
    except Exception as exc:
        raise SecureLoaderError(f"License token is not valid base64: {exc}") from None

    if not _verify_signature(manifest_bytes, signature_hex, public_keys):
        raise SecureLoaderError(
            "License signature did not validate against any known key"
        )

    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise SecureLoaderError(f"License manifest is not valid JSON: {exc}") from None

    if str(manifest.get("user_id")) != str(expected_user_id):
        raise SecureLoaderError("License user_id does not match the authenticated user")

    expires_at_raw = manifest.get("expires_at")
    if not expires_at_raw:
        raise SecureLoaderError("License manifest missing expires_at")
    if _parse_expires_at(expires_at_raw) <= check_time:
        raise SecureLoaderError("License has expired")

    return manifest


def _parse_artifact(artifact_bytes: bytes) -> tuple[dict, bytes]:
    if _NATIVE_AVAILABLE:
        try:
            return _native.parse_artifact(artifact_bytes)
        except _native.NativeSecureLoaderError as exc:
            raise _translate_native_error(exc) from exc
    if len(artifact_bytes) < 9:
        raise SecureLoaderError("Artifact too short")
    if artifact_bytes[:4] != ARTIFACT_MAGIC:
        raise SecureLoaderError("Artifact magic mismatch")
    format_version = artifact_bytes[4]
    if format_version != ARTIFACT_FORMAT_VERSION:
        raise SecureLoaderError(f"Unsupported artifact format version {format_version}")
    header_len = int.from_bytes(artifact_bytes[5:9], "big")
    header_bytes = artifact_bytes[9 : 9 + header_len]
    try:
        header = json.loads(header_bytes)
    except json.JSONDecodeError as exc:
        raise SecureLoaderError(f"Artifact header is not valid JSON: {exc}") from None
    ciphertext = artifact_bytes[9 + header_len :]
    return header, ciphertext


def _build_aad(model_key: str, version: str, master_key_id: str) -> bytes:
    if _NATIVE_AVAILABLE:
        return _native.build_aad(model_key, version, master_key_id)
    return _canonical_json(
        {
            "master_key_id": master_key_id,
            "model_key": model_key,
            "version": version,
        }
    )


def _decrypt_artifact(
    artifact_bytes: bytes,
    wrapped_key_hex: str,
    expected_model_key: str,
    expected_version: str,
) -> bytes:
    """Validate the artifact header against the license, then AES-GCM decrypt."""
    if _NATIVE_AVAILABLE:
        try:
            return _native.decrypt_artifact(
                artifact_bytes, wrapped_key_hex, expected_model_key, expected_version
            )
        except _native.NativeSecureLoaderError as exc:
            raise _translate_native_error(exc) from exc

    header, ciphertext = _parse_artifact(artifact_bytes)

    if header.get("model_key") != expected_model_key:
        raise SecureLoaderError("Artifact model_key does not match license")
    if header.get("version") != expected_version:
        raise SecureLoaderError("Artifact version does not match license")

    try:
        wrapped_key = bytes.fromhex(wrapped_key_hex)
    except ValueError as exc:
        raise SecureLoaderError(f"wrapped_key is not hex-encoded: {exc}") from None

    try:
        nonce = base64.b64decode(header["nonce_b64"])
    except (KeyError, Exception) as exc:
        raise SecureLoaderError(
            f"Artifact header missing/invalid nonce: {exc}"
        ) from None

    aad = _build_aad(header["model_key"], header["version"], header["master_key_id"])
    try:
        return AESGCM(wrapped_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SecureLoaderError("Artifact decryption failed (tag mismatch)") from exc


def _select_providers() -> list[str]:
    """Return the preferred-first list of available ONNX Runtime providers."""
    available = _onnxruntime().get_available_providers()
    preferred = (
        "CUDAExecutionProvider",
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    )
    chosen = [p for p in preferred if p in available]
    if not chosen:
        chosen = ["CPUExecutionProvider"]
    return chosen


def _download_artifact(url: str, http_client: Optional[httpx.Client] = None) -> bytes:
    """Fetch the encrypted artifact bytes from `url`."""
    client = http_client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        resp = client.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise SecureLoaderError(
                f"Artifact download failed (HTTP {resp.status_code}) for {url}"
            )
        return resp.content
    finally:
        if http_client is None:
            client.close()


class SecureLoader:
    """Acquire a license from TTT, download the encrypted artifact, decrypt it,
    and produce an `onnxruntime.InferenceSession` ready for inference.

    The TTT client must already be authenticated — `acquire(...)` raises if not.
    """

    def __init__(
        self,
        ttt_client,
        public_keys: list[str],
        http_client: Optional[httpx.Client] = None,
        state_storage_path: Optional[str] = None,
    ):
        self._ttt = ttt_client
        self._public_keys = list(public_keys)
        self._http = http_client
        # When set, every successful acquire is recorded for the tray UI.
        self._state_storage_path = state_storage_path

    def acquire(
        self,
        model_key: str,
        channel: Optional[str] = None,
        pipeline_version: Optional[str] = None,
    ) -> LoadedModel:
        if not self._ttt.is_authenticated():
            raise SecureLoaderError("TTT client is not authenticated")

        try:
            license_response = self._ttt.acquire_model_license(
                model_key, channel=channel, pipeline_version=pipeline_version
            )
        except Exception as exc:
            raise SecureLoaderError(
                f"Could not acquire license for {model_key}: {exc}"
            ) from exc

        user_id = self._ttt.current_user_id()
        if not user_id:
            raise SecureLoaderError("TTT client returned no user_id")

        manifest = _verify_license(license_response, self._public_keys, user_id)

        artifact_url = manifest.get("artifact_url")
        if not artifact_url:
            raise SecureLoaderError("License manifest missing artifact_url")
        artifact_bytes = _download_artifact(artifact_url, self._http)

        sha = manifest.get("artifact_sha256")
        if sha:
            actual = hashlib.sha256(artifact_bytes).hexdigest()
            if actual != sha:
                raise SecureLoaderError(
                    "Artifact SHA-256 does not match license manifest"
                )

        plaintext = _decrypt_artifact(
            artifact_bytes,
            license_response["wrapped_key"],
            manifest["model_key"],
            manifest["model_version"],
        )

        providers = _select_providers()
        try:
            session = _onnxruntime().InferenceSession(plaintext, providers=providers)
        except Exception as exc:
            raise SecureLoaderError(f"ONNX Runtime rejected the model: {exc}") from exc

        loaded = LoadedModel(
            session=session,
            model_key=manifest["model_key"],
            version=manifest["model_version"],
            tier=manifest.get("tier", "unknown"),
            provider=session.get_providers()[0]
            if session.get_providers()
            else "unknown",
        )

        # Record state for the tray UI (best-effort; failure must not break inference).
        if self._state_storage_path:
            try:
                from video_grouper.ball_tracking import license_state

                license_state.record(
                    self._state_storage_path,
                    model_key=loaded.model_key,
                    version=loaded.version,
                    tier=loaded.tier,
                    expires_at=manifest.get("expires_at", ""),
                )
            except Exception:
                logger.exception("Failed to record license state")

        return loaded
