# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: c_string_type=bytes
# cython: c_string_encoding=ascii

"""Native (Cython-compiled) implementations of the secure-loader hot paths.

When this module is compiled to a native extension (.pyd / .so / .dylib),
the security-critical glue — canonical JSON, AAD construction, manifest
verification, AES-GCM decrypt orchestration — runs as native code rather
than introspectable Python bytecode.

The pure-Python fallback in `secure_loader.py` is functionally identical;
this module exists for the obfuscation it provides at compile time, not
for behavior changes.

Compiled by CI via cython + a C compiler. Dev/test workflows can run
against the pure-Python fallback unchanged.
"""

import base64
import json
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


cdef bytes _MAGIC = b"TTBM"
cdef int _FORMAT_VERSION = 1


class NativeSecureLoaderError(Exception):
    """Raised when a model cannot be loaded — surfaced to Python as
    SecureLoaderError by the wrapper module."""


cpdef bytes canonical_json(object obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


cpdef object parse_expires_at(str value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    cdef object dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


cpdef bint verify_signature(bytes message, str signature_hex, list public_keys):
    cdef bytes sig
    try:
        sig = bytes.fromhex(signature_hex.strip())
    except ValueError:
        return False
    cdef str key_hex
    cdef object pub
    for key_hex in public_keys:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
            pub.verify(sig, message)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


cpdef dict verify_license(
    dict license_response,
    list public_keys,
    str expected_user_id,
    object now=None,
):
    cdef object check_time = now or datetime.now(UTC)
    cdef str token_b64
    cdef str signature_hex
    try:
        token_b64 = license_response["license_token"]
        signature_hex = license_response["license_signature"]
    except KeyError as exc:
        raise NativeSecureLoaderError(f"License response missing field: {exc}") from None

    cdef bytes manifest_bytes
    try:
        manifest_bytes = base64.b64decode(token_b64)
    except Exception as exc:
        raise NativeSecureLoaderError(f"License token is not valid base64: {exc}") from None

    if not verify_signature(manifest_bytes, signature_hex, public_keys):
        raise NativeSecureLoaderError(
            "License signature did not validate against any known key"
        )

    cdef dict manifest
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise NativeSecureLoaderError(
            f"License manifest is not valid JSON: {exc}"
        ) from None

    if str(manifest.get("user_id")) != str(expected_user_id):
        raise NativeSecureLoaderError(
            "License user_id does not match the authenticated user"
        )

    cdef object expires_at_raw = manifest.get("expires_at")
    if not expires_at_raw:
        raise NativeSecureLoaderError("License manifest missing expires_at")
    if parse_expires_at(expires_at_raw) <= check_time:
        raise NativeSecureLoaderError("License has expired")

    return manifest


cpdef tuple parse_artifact(bytes artifact_bytes):
    if len(artifact_bytes) < 9:
        raise NativeSecureLoaderError("Artifact too short")
    if artifact_bytes[:4] != _MAGIC:
        raise NativeSecureLoaderError("Artifact magic mismatch")
    cdef int format_version = artifact_bytes[4]
    if format_version != _FORMAT_VERSION:
        raise NativeSecureLoaderError(
            f"Unsupported artifact format version {format_version}"
        )
    cdef int header_len = int.from_bytes(artifact_bytes[5:9], "big")
    cdef bytes header_bytes = artifact_bytes[9 : 9 + header_len]
    cdef dict header
    try:
        header = json.loads(header_bytes)
    except json.JSONDecodeError as exc:
        raise NativeSecureLoaderError(
            f"Artifact header is not valid JSON: {exc}"
        ) from None
    cdef bytes ciphertext = artifact_bytes[9 + header_len :]
    return header, ciphertext


cpdef bytes build_aad(str model_key, str version, str master_key_id):
    return canonical_json(
        {
            "master_key_id": master_key_id,
            "model_key": model_key,
            "version": version,
        }
    )


cpdef bytes decrypt_artifact(
    bytes artifact_bytes,
    str wrapped_key_hex,
    str expected_model_key,
    str expected_version,
):
    cdef dict header
    cdef bytes ciphertext
    header, ciphertext = parse_artifact(artifact_bytes)

    if header.get("model_key") != expected_model_key:
        raise NativeSecureLoaderError("Artifact model_key does not match license")
    if header.get("version") != expected_version:
        raise NativeSecureLoaderError("Artifact version does not match license")

    cdef bytes wrapped_key
    try:
        wrapped_key = bytes.fromhex(wrapped_key_hex)
    except ValueError as exc:
        raise NativeSecureLoaderError(
            f"wrapped_key is not hex-encoded: {exc}"
        ) from None

    cdef bytes nonce
    try:
        nonce = base64.b64decode(header["nonce_b64"])
    except (KeyError, Exception) as exc:
        raise NativeSecureLoaderError(
            f"Artifact header missing/invalid nonce: {exc}"
        ) from None

    cdef bytes aad = build_aad(
        header["model_key"], header["version"], header["master_key_id"]
    )
    try:
        return AESGCM(wrapped_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise NativeSecureLoaderError(
            "Artifact decryption failed (tag mismatch)"
        ) from exc
