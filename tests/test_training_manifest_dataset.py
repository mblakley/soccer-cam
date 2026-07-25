"""Unit tests for the v3 far-ball recall dataset/config changes.

Covers the dataset-side knobs in ``training/data_prep/manifest.py``:
  - far row (0) is INCLUDED by default (it used to be unconditionally dropped),
  - the far-field positive multiplier is applied on top of the spatial weight,
  - camera-balanced sampling produces the expected effective weights from a
    small synthetic game registry,
and the train-config side in ``training/train.py``:
  - the v3 hyperparameters are exposed and wired into ``model.train``.

These tests touch no GPU, no real video, and no network — they build a tiny
SQLite manifest with synthetic tiles on a tmp_path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from training.data_prep import manifest as M

# ---------------------------------------------------------------------------
# Helpers — build a tiny manifest + synthetic tiles on disk
# ---------------------------------------------------------------------------


def _seed_manifest(db_path: Path, tiles_dir: Path, games: dict[str, list[dict]]):
    """Create a manifest.db and tile .jpgs for the given games.

    games: game_id -> list of tile specs. Each spec is
        {"segment", "frame", "row", "col", "label": bool}.
    A labeled tile gets one ball box; an unlabeled tile is a negative.
    """
    conn = M.open_db(db_path, create=True)
    for game_id, tiles in games.items():
        game_dir = tiles_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)
        M.upsert_game(conn, game_id, tile_dir=str(game_dir))

        label_rows = []
        for t in tiles:
            stem = f"{t['segment']}_frame_{t['frame']:06d}_r{t['row']}_c{t['col']}"
            # Write a (tiny, non-empty) jpg placeholder so existence checks pass.
            (game_dir / f"{stem}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
            if t.get("label"):
                # class 0 ball, centered, small box
                label_rows.append(
                    (game_id, stem, 0, 0.5, 0.5, 0.02, 0.02, "test", None)
                )
        if label_rows:
            M.bulk_insert_labels(conn, label_rows)
    conn.commit()
    return conn


def _count_emits(train_txt: Path) -> dict[str, int]:
    """Return {basename_stem: emit_count} from a generated train.txt."""
    counts: dict[str, int] = {}
    for line in train_txt.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        stem = Path(line).stem
        counts[stem] = counts.get(stem, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# A. Far row (0) is included by default
# ---------------------------------------------------------------------------


def test_default_exclude_rows_is_empty():
    """Row 0 must no longer be excluded by default."""
    assert M.DEFAULT_EXCLUDE_ROWS == set()
    assert 0 not in M.DEFAULT_EXCLUDE_ROWS


def test_far_row_included_in_dataset_by_default(tmp_path):
    """A far-row (r0) positive tile lands in train.txt with default args."""
    db = tmp_path / "manifest.db"
    tiles = tmp_path / "tiles"
    out = tmp_path / "out"

    games = {
        "flash__2024.06.01_vs_IYSA_home": [
            {"segment": "seg0", "frame": 0, "row": 0, "col": 3, "label": True},
            {"segment": "seg0", "frame": 4, "row": 2, "col": 3, "label": True},
        ]
    }
    conn = _seed_manifest(db, tiles, games)
    try:
        # No camera balancing, no far multiplier -> isolate row inclusion.
        M.build_dataset(
            conn,
            tiles_dir=tiles,
            output_dir=out,
            val_split=0.0,
            neg_ratio=0.0,
            include_negatives=False,
            far_positive_multiplier=1.0,
            game_formats=None,
        )
    finally:
        conn.close()

    counts = _count_emits(out / "train.txt")
    far_stems = [s for s in counts if "_r0_c" in s]
    assert far_stems, f"far row tile missing from train.txt: {list(counts)}"


def test_exclude_rows_still_drops_when_requested(tmp_path):
    """Explicitly excluding row 0 still removes it (backward compat)."""
    db = tmp_path / "manifest.db"
    tiles = tmp_path / "tiles"
    out = tmp_path / "out"

    games = {
        "flash__2024.06.01_vs_IYSA_home": [
            {"segment": "seg0", "frame": 0, "row": 0, "col": 3, "label": True},
            {"segment": "seg0", "frame": 4, "row": 2, "col": 3, "label": True},
        ]
    }
    conn = _seed_manifest(db, tiles, games)
    try:
        M.build_dataset(
            conn,
            tiles_dir=tiles,
            output_dir=out,
            val_split=0.0,
            neg_ratio=0.0,
            include_negatives=False,
            exclude_rows={0},
            far_positive_multiplier=1.0,
            game_formats=None,
        )
    finally:
        conn.close()

    counts = _count_emits(out / "train.txt")
    assert not [s for s in counts if "_r0_c" in s]
    assert [s for s in counts if "_r2_c" in s]


# ---------------------------------------------------------------------------
# B. Far-field positive multiplier
# ---------------------------------------------------------------------------


def test_far_positive_multiplier_applied(tmp_path):
    """A far positive is emitted ~mult x the equivalent near positive.

    Use the same spatial tile weight on both rows so the only difference is the
    far multiplier. (0,3) and (1,3) both have DEFAULT_TILE_WEIGHTS == 2.
    """
    db = tmp_path / "manifest.db"
    tiles = tmp_path / "tiles"
    out = tmp_path / "out"

    assert M.DEFAULT_TILE_WEIGHTS[(0, 3)] == M.DEFAULT_TILE_WEIGHTS[(1, 3)]

    games = {
        "flash__2024.06.01_vs_IYSA_home": [
            {"segment": "seg0", "frame": 0, "row": 0, "col": 3, "label": True},
            {"segment": "seg0", "frame": 4, "row": 1, "col": 3, "label": True},
        ]
    }
    conn = _seed_manifest(db, tiles, games)
    try:
        M.build_dataset(
            conn,
            tiles_dir=tiles,
            output_dir=out,
            val_split=0.0,
            neg_ratio=0.0,
            include_negatives=False,
            far_positive_multiplier=4.0,
            game_formats=None,
        )
    finally:
        conn.close()

    counts = _count_emits(out / "train.txt")
    far = next(c for s, c in counts.items() if "_r0_c3" in s)
    mid = next(c for s, c in counts.items() if "_r1_c3" in s)
    # base weight 2 each; far gets x4 -> 8 vs 2.
    assert mid == 2
    assert far == 8
    assert far == mid * 4


# ---------------------------------------------------------------------------
# C. Camera-balanced sampling
# ---------------------------------------------------------------------------


def test_classify_camera():
    assert M.classify_camera("reolink_segments") == "reolink"
    assert M.classify_camera("dahua_segments") == "dahua"
    assert M.classify_camera("dav_only") == "dahua"
    assert M.classify_camera("gopro") == "other"
    assert M.classify_camera(None) == "other"


def test_compute_camera_weights_balanced():
    """81:1 Dahua:Reolink tiles, target ratio 1.0 -> Reolink up-weighted 81x."""
    # 3 dahua games (27 tiles each = 81), 1 reolink game (1 tile).
    tile_counts = {
        "d1": 27,
        "d2": 27,
        "d3": 27,
        "r1": 1,
    }
    formats = {
        "d1": "dahua_segments",
        "d2": "dahua_segments",
        "d3": "dav_only",
        "r1": "reolink_segments",
    }
    w = M.compute_camera_weights(tile_counts, formats, target_ratio=1.0)

    # Reolink anchored at 1.0; dahua scaled so effective contributions match.
    assert w["r1"] == pytest.approx(1.0)
    # w_dahua = target * n_reo / n_dahua = 1.0 * 1 / 81
    assert w["d1"] == pytest.approx(1.0 / 81.0)
    assert w["d1"] == w["d2"] == w["d3"]

    # Effective contributions are now equal (balanced).
    eff_dahua = sum(tile_counts[g] * w[g] for g in ("d1", "d2", "d3"))
    eff_reo = tile_counts["r1"] * w["r1"]
    assert eff_dahua == pytest.approx(eff_reo)


def test_compute_camera_weights_reolink_favored():
    """target_ratio 0.5 makes Reolink dominate 2:1."""
    tile_counts = {"d1": 80, "r1": 1}
    formats = {"d1": "dahua_segments", "r1": "reolink_segments"}
    w = M.compute_camera_weights(tile_counts, formats, target_ratio=0.5)

    eff_dahua = tile_counts["d1"] * w["d1"]
    eff_reo = tile_counts["r1"] * w["r1"]
    # Reolink should contribute 2x Dahua (ratio dahua:reolink == 0.5).
    assert eff_reo == pytest.approx(2.0 * eff_dahua)


def test_compute_camera_weights_no_balance_when_one_class_absent():
    """All-Dahua set: nothing to balance, everyone gets weight 1.0."""
    tile_counts = {"d1": 10, "d2": 5}
    formats = {"d1": "dahua_segments", "d2": "dav_only"}
    w = M.compute_camera_weights(tile_counts, formats, target_ratio=1.0)
    assert w == {"d1": 1.0, "d2": 1.0}


def test_camera_balance_in_build_dataset(tmp_path):
    """End-to-end: a lone Reolink positive out-emits a Dahua positive.

    Equal per-tile spatial weight on both (use row 2, col 3 -> weight 2). With
    81 Dahua positive tiles vs 1 Reolink, balanced sampling should up-weight the
    Reolink tile dramatically relative to each Dahua tile.
    """
    db = tmp_path / "manifest.db"
    tiles = tmp_path / "tiles"
    out = tmp_path / "out"

    dahua_id = "flash__2024.06.01_vs_IYSA_home"  # many tiles
    reo_id = "heat__2025.06.02_vs_Fairport_home"  # one tile

    dahua_tiles = [
        {"segment": "seg0", "frame": i, "row": 2, "col": 3, "label": True}
        for i in range(81)
    ]
    games = {
        dahua_id: dahua_tiles,
        reo_id: [{"segment": "seg0", "frame": 0, "row": 2, "col": 3, "label": True}],
    }
    formats = {dahua_id: "dahua_segments", reo_id: "reolink_segments"}

    conn = _seed_manifest(db, tiles, games)
    try:
        # Force both games into the train split (val_split=0) and disable far
        # multiplier + negatives so camera balance is the only effect.
        M.build_dataset(
            conn,
            tiles_dir=tiles,
            output_dir=out,
            val_split=0.0,
            neg_ratio=0.0,
            include_negatives=False,
            far_positive_multiplier=1.0,
            game_formats=formats,
            camera_balance_ratio=1.0,
        )
    finally:
        conn.close()

    counts = _count_emits(out / "train.txt")
    reo_emits = next(c for s, c in counts.items() if s.startswith("seg0_frame_000000"))
    # The single Reolink tile and the frame-0 Dahua tile share the stem prefix
    # but live in different game subdirs; disambiguate via the emitted paths.
    reo_lines = [
        ln for ln in (out / "train.txt").read_text().splitlines() if reo_id in ln
    ]
    dahua_frame0_lines = [
        ln
        for ln in (out / "train.txt").read_text().splitlines()
        if dahua_id in ln and "frame_000000" in ln
    ]
    # base spatial weight is 2; reolink anchored at 1.0 -> 2 emits.
    assert len(reo_lines) == 2
    # each dahua tile weight = 2 * (1/81) -> rounds to 1 (never below 1).
    assert len(dahua_frame0_lines) == 1
    assert len(reo_lines) > len(dahua_frame0_lines)
    assert reo_emits  # sanity: stem prefix accounting works


# ---------------------------------------------------------------------------
# D. train.py exposes the v3 hyperparameters
# ---------------------------------------------------------------------------


def test_train_module_constants():
    from training import train as T

    assert T.V3_MODEL == "yolo26l.pt"
    assert T.V3_MULTI_SCALE == 0.5
    assert T.V3_MOSAIC == 0.5
    assert T.V3_COPY_PASTE == 0.3
    assert T.V3_CLS == 1.5
    assert T.V3_COS_LR is True
    assert 0.001 <= T.V3_LR0 <= 0.005
    assert T.V3_PATIENCE == 15
    assert 5 <= T.V3_FREEZE <= 10
    assert T.V3_HSV_V < 0.4  # lowered from v2's 0.4


def test_train_signature_defaults():
    from training import train as T

    sig = inspect.signature(T.train)
    p = sig.parameters
    assert p["model_name"].default == "yolo26l.pt"
    assert p["multi_scale"].default == 0.5
    assert p["mosaic"].default == 0.5
    assert p["copy_paste"].default == 0.3
    assert p["cls"].default == 1.5
    assert p["cos_lr"].default is True
    assert p["patience"].default == 15
    assert 5 <= p["freeze"].default <= 10
    assert p["hsv_v"].default < 0.4
    assert 0.001 <= p["lr0"].default <= 0.005


def test_train_passes_v3_args_to_model(monkeypatch, tmp_path):
    """train() forwards the v3 knobs into model.train(**kwargs).

    ``ultralytics`` is a GPU/training-only dependency and is not installed in
    the base test environment, so we inject a fake module into sys.modules. The
    ``train()`` body does ``from ultralytics import YOLO`` at call time, so this
    fake is what it picks up.
    """
    import sys
    import types

    from training import train as T

    captured = {}

    class _FakeResults:
        pass

    class _FakeModel:
        def __init__(self, name):
            captured["model_name"] = name

        def add_callback(self, *a, **k):
            pass

        def train(self, **kwargs):
            captured.update(kwargs)
            return _FakeResults()

    fake_ultra = types.ModuleType("ultralytics")
    fake_ultra.YOLO = _FakeModel
    monkeypatch.setitem(sys.modules, "ultralytics", fake_ultra)

    data_yaml = tmp_path / "dataset.yaml"
    data_yaml.write_text("path: .\ntrain: train.txt\nval: val.txt\nnc: 1\n")

    T.train(data_yaml, epochs=1)

    assert captured["model_name"] == "yolo26l.pt"
    assert captured["multi_scale"] == 0.5
    assert captured["mosaic"] == 0.5
    assert captured["copy_paste"] == 0.3
    assert captured["cls"] == 1.5
    assert captured["cos_lr"] is True
    assert captured["lr0"] == 0.002
    assert captured["patience"] == 15
    assert 5 <= captured["freeze"] <= 10
    assert captured["hsv_v"] < 0.4
