"""Immutable, versioned crop-store indexes — every dataset state is reproducible.

Why (EXP-DIST-55): ``crops_reolink`` was mutated IN PLACE by successive mining
rounds, so "the store" silently changed identity between runs — an entire
experiment batch trained on hn5's data while believing it was hn4's, and the
only recovery path was an ad-hoc backup file the miner happened to write.

Rules enforced here:

- ``index_vN.json`` files are IMMUTABLE snapshots. Freezing identical content
  twice returns the same version (content-addressed by sha, not by count).
- ``index.json`` stays the CURRENT alias — every existing reader keeps working —
  but mutators MUST pin the store before and after an edit
  (:func:`freeze_index` both sides), so both states are recoverable forever.
- Consumers (datasets → trainers → checkpoints) record ``(version, sha)`` so
  every artifact knows exactly which data produced it.
- Crop ``.npy`` files are never deleted; ``index_vN.json`` files are never
  edited.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def canonical_sha(obj) -> str:
    """Content hash of an index (key-order independent, 16 hex chars)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _versions(store: Path) -> dict[int, Path]:
    out: dict[int, Path] = {}
    for p in store.glob("index_v*.json"):
        m = re.fullmatch(r"index_v(\d+)\.json", p.name)
        if m:
            out[int(m.group(1))] = p
    return out


def freeze_index(store) -> tuple[int, str]:
    """Pin the store's current ``index.json`` as an immutable ``index_vN.json``.

    Idempotent: if the current content already matches a pinned version, that
    version is returned. Returns ``(version, sha)``; on an unwritable store the
    version is 0 (= "unpinned") so read-only consumers still work — callers
    surface that in their logs.
    """
    store = Path(store)
    cur = json.loads((store / "index.json").read_text())
    sha = canonical_sha(cur)
    vers = _versions(store)
    for n in sorted(vers):
        if canonical_sha(json.loads(vers[n].read_text())) == sha:
            return n, sha
    n = max(vers, default=0) + 1
    try:
        tmp = store / f"index_v{n}.json.tmp"
        tmp.write_text(json.dumps(cur))
        tmp.replace(store / f"index_v{n}.json")
    except OSError:
        return 0, sha
    return n, sha


def resolve_index(store, version: int | None = None) -> tuple[dict | list, int, str]:
    """Load a pinned index: ``(data, version, sha)``.

    ``version=None`` freezes and uses the CURRENT ``index.json`` — the default
    path automatically pins provenance for every run. An explicit ``version``
    loads that exact immutable snapshot regardless of later mutations.
    """
    store = Path(store)
    if version is None:
        n, sha = freeze_index(store)
        return json.loads((store / "index.json").read_text()), n, sha
    p = store / f"index_v{version}.json"
    data = json.loads(p.read_text())
    return data, version, canonical_sha(data)
