"""Persistent machine identifier for multi-computer awareness."""

import hashlib
import platform
import uuid
from pathlib import Path


def _get_hardware_id() -> str | None:
    """Derive a stable ID from the Windows MachineGuid registry key.

    Returns None on non-Windows platforms or if the key is unreadable.
    """
    if platform.system() != "Windows":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        )
        machine_guid = winreg.QueryValueEx(key, "MachineGuid")[0]
        winreg.CloseKey(key)
        # Hash to a UUID-shaped string so the format is consistent
        digest = hashlib.sha256(machine_guid.encode()).hexdigest()
        return str(uuid.UUID(digest[:32]))
    except Exception:
        return None


def get_or_create_machine_id(storage_path: str) -> str:
    """Get a stable machine identifier.

    Priority:
    1. Hardware-based ID (Windows MachineGuid) — survives reinstalls.
    2. Persisted UUID in ``{storage_path}/machine_id`` — fallback.
    3. New random UUID (written to disk for next time).
    """
    # Prefer hardware ID so the same computer always returns the same value
    hw_id = _get_hardware_id()
    if hw_id:
        return hw_id

    # Fallback: file-based persistence
    id_file = Path(storage_path) / "machine_id"
    if id_file.exists():
        return id_file.read_text().strip()
    machine_id = str(uuid.uuid4())
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(machine_id)
    return machine_id
