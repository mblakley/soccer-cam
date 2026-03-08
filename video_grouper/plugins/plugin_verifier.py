"""Verify HMAC-SHA256 signatures of plugin zip packages."""

import hashlib
import hmac
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def verify_plugin_signature(zip_path: Path, signing_key: str) -> bool:
    """
    Verify HMAC-SHA256 signature of a plugin zip.

    The zip contains a 'signature' file with the expected HMAC.
    The HMAC is computed over all other files in the zip (sorted by name).
    Returns False on any error (fail-safe).
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "signature" not in zf.namelist():
                logger.warning("Plugin %s has no signature file", zip_path.name)
                return False

            sig_data = zf.read("signature").decode().strip()

            mac = hmac.new(signing_key.encode(), digestmod=hashlib.sha256)
            for name in sorted(zf.namelist()):
                if name == "signature":
                    continue
                mac.update(zf.read(name))

            if hmac.compare_digest(mac.hexdigest(), sig_data):
                return True

            logger.warning("Plugin %s signature mismatch", zip_path.name)
            return False
    except Exception:
        logger.exception("Failed to verify plugin %s", zip_path.name)
        return False
