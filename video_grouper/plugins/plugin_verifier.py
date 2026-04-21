"""Verify Ed25519 signatures on plugin manifests."""

import hashlib
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_expires_at(value: str) -> datetime:
    # Accept trailing 'Z' or explicit offset; normalize to aware UTC.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _verify_signature(
    manifest_bytes: bytes, signature_hex: str, public_keys: list[str]
) -> bool:
    try:
        signature = bytes.fromhex(signature_hex.strip())
    except ValueError:
        return False
    for key_hex in public_keys:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
            pub.verify(signature, manifest_bytes)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def verify_plugin_bundle(
    zip_path: Path,
    public_keys: list[str],
    current_user_id: str,
    now: Optional[datetime] = None,
) -> bool:
    """Verify a downloaded plugin zip before extraction.

    Checks: signature over manifest.json, user_id binding, not-expired,
    and per-file sha256 of every entry listed in the manifest.
    Returns False on any failure. Errors are logged with mechanical messages.
    """
    check_time = now or _now()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names or "manifest.json.sig" not in names:
                logger.warning("Plugin %s missing manifest or signature", zip_path.name)
                return False

            manifest_bytes = zf.read("manifest.json")
            signature_hex = zf.read("manifest.json.sig").decode().strip()

            if not _verify_signature(manifest_bytes, signature_hex, public_keys):
                logger.warning("Plugin %s signature mismatch", zip_path.name)
                return False

            try:
                manifest = json.loads(manifest_bytes)
            except json.JSONDecodeError:
                logger.warning("Plugin %s manifest is not valid JSON", zip_path.name)
                return False

            if manifest.get("user_id") != current_user_id:
                logger.warning("Plugin %s user_id mismatch", zip_path.name)
                return False

            expires_at_raw = manifest.get("expires_at")
            if not expires_at_raw:
                logger.warning("Plugin %s manifest missing expires_at", zip_path.name)
                return False
            try:
                expires_at = _parse_expires_at(expires_at_raw)
            except ValueError:
                logger.warning("Plugin %s expires_at is not ISO 8601", zip_path.name)
                return False
            if expires_at <= check_time:
                logger.warning("Plugin %s manifest expired", zip_path.name)
                return False

            files = manifest.get("files")
            if not isinstance(files, list):
                logger.warning("Plugin %s manifest has no files list", zip_path.name)
                return False
            for entry in files:
                path = entry.get("path")
                expected = entry.get("sha256")
                if not path or not expected:
                    logger.warning(
                        "Plugin %s manifest entry missing path/sha256", zip_path.name
                    )
                    return False
                if path not in names:
                    logger.warning(
                        "Plugin %s missing file listed in manifest: %s",
                        zip_path.name,
                        path,
                    )
                    return False
                actual = hashlib.sha256(zf.read(path)).hexdigest()
                if actual != expected:
                    logger.warning(
                        "Plugin %s file hash mismatch: %s", zip_path.name, path
                    )
                    return False

        return True
    except zipfile.BadZipFile:
        logger.warning("Plugin %s is not a valid zip", zip_path.name)
        return False
    except Exception:
        logger.exception("Failed to verify plugin %s", zip_path.name)
        return False


def verify_extracted_plugin(
    plugin_root: Path,
    public_keys: list[str],
    current_user_id: str,
    now: Optional[datetime] = None,
) -> bool:
    """Re-verify a plugin that has already been extracted to disk.

    plugin_root contains manifest.json, manifest.json.sig, and plugin/<files>.
    Run at load time so expired or tampered manifests are refused even after
    the original download passed.
    """
    check_time = now or _now()
    manifest_path = plugin_root / "manifest.json"
    sig_path = plugin_root / "manifest.json.sig"
    if not manifest_path.is_file() or not sig_path.is_file():
        logger.warning("Plugin %s missing manifest or signature", plugin_root.name)
        return False

    try:
        manifest_bytes = manifest_path.read_bytes()
        signature_hex = sig_path.read_text().strip()

        if not _verify_signature(manifest_bytes, signature_hex, public_keys):
            logger.warning("Plugin %s signature mismatch", plugin_root.name)
            return False

        manifest = json.loads(manifest_bytes)

        if manifest.get("user_id") != current_user_id:
            logger.warning("Plugin %s user_id mismatch", plugin_root.name)
            return False

        try:
            expires_at = _parse_expires_at(manifest["expires_at"])
        except (KeyError, ValueError):
            logger.warning("Plugin %s expires_at missing or invalid", plugin_root.name)
            return False
        if expires_at <= check_time:
            logger.warning("Plugin %s manifest expired", plugin_root.name)
            return False

        for entry in manifest.get("files", []):
            rel = entry.get("path")
            expected = entry.get("sha256")
            if not rel or not expected:
                logger.warning(
                    "Plugin %s manifest entry missing path/sha256", plugin_root.name
                )
                return False
            file_path = plugin_root / rel
            if not file_path.is_file():
                logger.warning("Plugin %s missing file: %s", plugin_root.name, rel)
                return False
            actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if actual != expected:
                logger.warning(
                    "Plugin %s file hash mismatch: %s", plugin_root.name, rel
                )
                return False

        return True
    except Exception:
        logger.exception("Failed to re-verify plugin %s", plugin_root.name)
        return False


def read_manifest_expires_at(manifest_path: Path) -> Optional[datetime]:
    """Read the expires_at timestamp from an on-disk manifest. None on failure."""
    try:
        data = json.loads(manifest_path.read_bytes())
        return _parse_expires_at(data["expires_at"])
    except Exception:
        return None
