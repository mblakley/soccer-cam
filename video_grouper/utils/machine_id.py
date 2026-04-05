"""Persistent machine identifier for multi-computer awareness."""

import uuid
from pathlib import Path


def get_or_create_machine_id(storage_path: str) -> str:
    """Get or create a persistent machine UUID for this installation.

    The ID is stored in a file in the storage directory so it survives
    app restarts but is unique per installation.
    """
    id_file = Path(storage_path) / "machine_id"
    if id_file.exists():
        return id_file.read_text().strip()
    machine_id = str(uuid.uuid4())
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(machine_id)
    return machine_id
