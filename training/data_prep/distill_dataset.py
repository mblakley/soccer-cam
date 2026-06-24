"""Build the distillation ``games`` list for :func:`build_heatmap_crops` from a reference per-frame
ball stream — AutoCam's ``{f, xy}`` sidecar, i.e. *what it feeds the renderer*.

Design (matches the capture-broadly / filter-after approach):

1. **Capture is the reference stream as-is** — the sidecar's per-frame ``{f, xy}`` ball target (source
   px). Frame ``f`` is 1-based and indexes the source panorama ``-raw.mp4`` as ``f-1`` (the raw is a
   few frames longer: AutoCam end-trims ~40-70 frames; those have no label and are skipped).
2. **Filter afterward (tunable)** — drop the *frozen/held* runs (where the stream stops moving =
   AutoCam lost the ball and is holding a stale position) and any off-field points; these are the
   labels we must NOT distill. The active stream is a smoothed-but-on-ball target (~2-3 px/frame), a
   fine per-frame label.
3. **Subsample** (the detector has no temporal target, so adjacent frames are near-duplicates), with
   densification near direction changes (the most appearance-varied, valuable frames).
4. **Merge human far-ball overrides** — they win on conflict and rescue frames the stream dropped.

This module names no external tool or model: it consumes a generic reference-detection ``.jsonl``
(``{"f": <1-based>, "xy": [x, y]}`` per line) plus an optional field polygon, and returns the
``games`` list-of-dicts that ``build_heatmap_crops`` renders. Eval-set frames are asserted out.
"""

from __future__ import annotations

import json
import math

import numpy as np


def load_reference_stream(jsonl_path) -> dict[int, tuple[float, float]]:
    """Parse a reference ``{f, xy}`` sidecar into ``{frame0: (x, y)}`` (0-based = ``f-1``).

    Tolerates non-label lines (e.g. ``{"lines": [...]}`` log records) by skipping any line without
    both ``f`` and ``xy``.
    """
    stream: dict[int, tuple[float, float]] = {}
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "f" in o and "xy" in o and o["xy"] is not None:
                stream[int(o["f"]) - 1] = (float(o["xy"][0]), float(o["xy"][1]))
    return stream


def drop_frozen_runs(
    stream: dict[int, tuple[float, float]],
    *,
    vel_px: float = 1.5,
    min_run: int = 20,
) -> tuple[dict[int, tuple[float, float]], int]:
    """Drop frames inside a sustained-frozen run — AutoCam holding a stale position when the ball is
    lost. A run is ``>= min_run`` consecutive frames each moving ``< vel_px`` from the previous.

    Returns ``(filtered_stream, n_dropped)``. Brief pauses (a real dead ball that AutoCam still has)
    stay — only *sustained* holds are removed. Operates on the dense (un-subsampled) series so runs
    are detected correctly.
    """
    frames = sorted(stream)
    if not frames:
        return dict(stream), 0
    # per-frame "moved?" relative to the previous *consecutive* frame
    slow = {}
    for i, f in enumerate(frames):
        if i == 0:
            slow[f] = False
            continue
        pf = frames[i - 1]
        d = math.hypot(stream[f][0] - stream[pf][0], stream[f][1] - stream[pf][1])
        # only treat as part of a hold if the previous frame is contiguous-ish (no big time gap)
        slow[f] = (f - pf) <= 4 and d < vel_px
    # find maximal runs of slow frames; drop those >= min_run
    drop: set[int] = set()
    i = 0
    n = len(frames)
    while i < n:
        if slow[frames[i]]:
            j = i
            while j < n and slow[frames[j]]:
                j += 1
            if (j - i) >= min_run:
                drop.update(frames[i:j])
            i = j
        else:
            i += 1
    return {f: xy for f, xy in stream.items() if f not in drop}, len(drop)


def drop_offfield(
    stream: dict[int, tuple[float, float]],
    polygon,
    *,
    margin_px: float = 60.0,
) -> tuple[dict[int, tuple[float, float]], int]:
    """Drop points outside the field polygon (point-in-polygon with a generous inward margin).

    ``polygon`` is an (N, 2) array of source-px field-boundary points. A negative ``pointPolygonTest``
    distance beyond ``-margin_px`` is off-field. If ``polygon`` is None, no-op.
    """
    if polygon is None:
        return dict(stream), 0
    import cv2

    poly = np.asarray(polygon, dtype=np.float32)
    kept, dropped = {}, 0
    for f, (x, y) in stream.items():
        if cv2.pointPolygonTest(poly, (float(x), float(y)), True) >= -margin_px:
            kept[f] = (x, y)
        else:
            dropped += 1
    return kept, dropped


def subsample(
    stream: dict[int, tuple[float, float]],
    *,
    base_stride: int = 4,
    dense_stride: int = 2,
    turn_deg: float = 45.0,
    turn_window: int = 10,
) -> dict[int, tuple[float, float]]:
    """Thin the dense stream: keep every ``base_stride``-th frame, but densify to ``dense_stride``
    within ``turn_window`` frames of a direction change (> ``turn_deg`` heading turn) — those carry
    the most varied ball appearance (decelerating / at feet)."""
    frames = sorted(stream)
    if not frames:
        return {}
    # heading at each frame from the displacement to the previous frame
    head = {}
    for i, f in enumerate(frames):
        if i == 0:
            head[f] = None
            continue
        pf = frames[i - 1]
        dx, dy = stream[f][0] - stream[pf][0], stream[f][1] - stream[pf][1]
        head[f] = math.atan2(dy, dx) if (dx or dy) else head[pf]
    turn_frames = set()
    for i in range(1, len(frames)):
        a, b = head[frames[i - 1]], head[frames[i]]
        if a is None or b is None:
            continue
        d = abs(math.degrees(b - a))
        d = min(d, 360 - d)
        if d > turn_deg:
            lo = max(0, i - turn_window)
            hi = min(len(frames), i + turn_window)
            turn_frames.update(frames[lo:hi])
    out = {}
    for f in frames:
        stride = dense_stride if f in turn_frames else base_stride
        if f % stride == 0:
            out[f] = stream[f]
    return out


def merge_overrides(
    stream: dict[int, tuple[float, float]],
    human: dict[int, tuple[float, float]] | None,
) -> dict[int, tuple[float, float]]:
    """Human far-ball labels win on conflict and rescue frames the reference stream dropped."""
    out = dict(stream)
    if human:
        out.update(human)
    return out


def _assert_no_eval_leak(labels: dict, game_id: str, exclude: dict) -> None:
    """Fail loudly if any excluded (held-out eval) frame survived into the training labels."""
    if game_id in exclude.get("game_ids", set()):
        raise AssertionError(
            f"{game_id} is a held-out eval game and must not be in training"
        )
    for lo, hi in exclude.get("frame_ranges", {}).get(game_id, []):
        leaked = [f for f in labels if lo <= f <= hi]
        if leaked:
            raise AssertionError(
                f"{game_id}: {len(leaked)} labels in held-out eval range {lo}..{hi} (e.g. {leaked[:5]})"
            )


def build_distill_games(
    game_configs: list[dict],
    *,
    exclude: dict | None = None,
    base_stride: int = 4,
    dense_stride: int = 2,
    frozen_vel_px: float = 1.5,
    frozen_min_run: int = 20,
    offfield_margin_px: float = 60.0,
    report: bool = True,
) -> list[dict]:
    """Turn per-game ``{game_id, video, sidecar, polygon?, human_labels?, split?, camera?, team?,
    target_width?}`` configs into the ``games`` list-of-dicts consumed by ``build_heatmap_crops``.

    Each output game: ``{game_id, video, polygon, labels:{frame:(x,y)}, split, target_width?}``.
    """
    exclude = exclude or {"game_ids": set(), "frame_ranges": {}}
    games: list[dict] = []
    for gc in game_configs:
        gid = gc["game_id"]
        if gid in exclude.get("game_ids", set()):
            if report:
                print(f"{gid}: SKIP (held-out eval game)")
            continue
        stream = load_reference_stream(gc["sidecar"])
        n0 = len(stream)
        stream, n_frozen = drop_frozen_runs(
            stream, vel_px=frozen_vel_px, min_run=frozen_min_run
        )
        polygon = gc.get("polygon")
        stream, n_off = drop_offfield(stream, polygon, margin_px=offfield_margin_px)
        # strip any excluded eval frames BEFORE subsampling so they can never appear
        for lo, hi in exclude.get("frame_ranges", {}).get(gid, []):
            stream = {f: xy for f, xy in stream.items() if not (lo <= f <= hi)}
        kept = subsample(stream, base_stride=base_stride, dense_stride=dense_stride)
        labels = merge_overrides(kept, gc.get("human_labels"))
        _assert_no_eval_leak(labels, gid, exclude)
        if not labels:
            if report:
                print(f"{gid}: 0 labels after filtering — skipped")
            continue
        out = {
            "game_id": gid,
            "video": gc["video"],
            "polygon": polygon if polygon is not None else gc.get("polygon"),
            "labels": labels,
            "split": gc.get("split", "train"),
        }
        if gc.get("target_width"):
            out["target_width"] = gc["target_width"]
        games.append(out)
        if report:
            print(
                f"{gid} [{gc.get('camera', '?')}/{gc.get('team', '?')}]: "
                f"{n0} ref -> drop {n_frozen} frozen, {n_off} off-field -> {len(kept)} subsampled "
                f"-> {len(labels)} labels (+{len(labels) - len(kept)} human)"
            )
    return games
