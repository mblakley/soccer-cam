"""Generate pre-warped YOLO datasets for v4 warp experiments.

We compare warp variants empirically (E1 showed ball pixels are too small + the
reference detector too noisy to pick a warp by geometry alone — see
V4_EXPERIMENT_TRACKER.md). This module pre-warps frames offline and writes a
**standard YOLO dataset** (images/ + labels/ + dataset.yaml) so the stock
ultralytics trainer can train each warp×resolution candidate with no custom
loader. Ball boxes are mapped through the *same* warp as the frame (by warping
the source box corners), so an anisotropic warp's elliptical near-ball gets a
correct wide box.

Warp variants (common interface: `.frame(img)`, `.points(xy)`, `.shape`):
- **W0 `crop_iso`** — crop the field band, isotropic (uniform x/y) resize to
  target_width. Balls stay round; size varies with depth.
- **W1 `aniso`** — the landed `field_warp` (vertical anisotropic compression):
  uniform vertical ball extent, near balls become wide ellipses.

Labels:
- **Reolink (no human labels)** — bootstrap from field-masked reference-detector
  detections (in-field, conf-filtered). Noisy near/mid; far balls need the human
  labeling loop (E4) — this is a bootstrap for the warp comparison, not final.
- **Dahua** — reuse existing verified labels, mapped through the warp.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from training.data_prep.field_warp import build_field_warp
from training.data_prep.field_warp import warp_frame as _aniso_frame
from training.data_prep.field_warp import warp_points as _aniso_points
from training.data_prep.warped_pack import _pad_to_stride

# Validated Reolink 05-27 ball-size gradient (EXPERIMENTS v2): far→near px by row.
DEFAULT_REOLINK_GRADIENT = (
    np.array([552.0, 912.0, 1272.0, 1452.0]),
    np.array([8.5, 11.75, 21.0, 33.2]),
)


# ---------------------------------------------------------------------------
# Warp variants (common interface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CropIsoWarp:
    """W0: crop the field band + isotropic resize to target_width (round balls)."""

    src_w: int
    src_h: int
    y_top: int
    y_bot: int
    target_width: int

    @property
    def scale(self) -> float:
        return self.target_width / self.src_w

    @property
    def final_h(self) -> int:
        return max(1, int(round((self.y_bot - self.y_top + 1) * self.scale)))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.final_h, self.target_width)

    def frame(self, img: np.ndarray) -> np.ndarray:
        import cv2

        band = img[self.y_top : self.y_bot + 1]
        return cv2.resize(
            band, (self.target_width, self.final_h), interpolation=cv2.INTER_AREA
        )

    def points(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        return np.column_stack(
            [xy[:, 0] * self.scale, (xy[:, 1] - self.y_top) * self.scale]
        )


class AnisoWarp:
    """W1: adapter over the landed FieldWarp (vertical anisotropic warp)."""

    def __init__(self, fw):
        self.fw = fw

    @property
    def shape(self) -> tuple[int, int]:
        return (self.fw.final_h, self.fw.target_width)

    def frame(self, img: np.ndarray) -> np.ndarray:
        return _aniso_frame(img, self.fw)

    def points(self, xy: np.ndarray) -> np.ndarray:
        return _aniso_points(np.asarray(xy, dtype=np.float64).reshape(-1, 2), self.fw)


def make_warp(kind: str, rows, sizes, src_w: int, src_h: int, target_width: int):
    """Build a warp variant. `kind` in {'crop_iso','aniso'}."""
    rows = np.asarray(rows, dtype=np.float64)
    if kind == "crop_iso":
        return CropIsoWarp(
            int(src_w),
            int(src_h),
            int(np.floor(rows.min())),
            int(np.ceil(rows.max())),
            int(target_width),
        )
    if kind == "aniso":
        return AnisoWarp(
            build_field_warp(
                rows, np.asarray(sizes, float), src_w, src_h, target_width=target_width
            )
        )
    raise ValueError(f"unknown warp kind {kind!r}")


# ---------------------------------------------------------------------------
# Field band + label bootstrap
# ---------------------------------------------------------------------------


def field_band_from_polygon(polygon, margin: int = 20) -> tuple[int, int]:
    """(y_top, y_bot) of the field band from the field-outline polygon (+margin)."""
    ys = np.asarray(polygon, dtype=np.float64)[:, 1]
    return int(max(0, np.floor(ys.min()) - margin)), int(np.ceil(ys.max()) + margin)


def in_field(polygon, cx: float, cy: float) -> bool:
    import cv2

    poly = np.asarray(polygon, dtype=np.float32)
    return cv2.pointPolygonTest(poly, (float(cx), float(cy)), False) >= 0


def bootstrap_labels(
    detections, polygon, conf_thresh: float = 0.6
) -> dict[int, list[tuple[float, float]]]:
    """Field-masked, conf-filtered reference detections → {frame_idx: [(cx,cy),...]}.

    The field mask is mandatory: E1 found ~80% of reference detections are
    off-field false positives (sky/trees/spectators).
    """
    out: dict[int, list[tuple[float, float]]] = {}
    for d in detections:
        if d.get("conf", 1.0) < conf_thresh:
            continue
        cx, cy = float(d["cx"]), float(d["cy"])
        if not in_field(polygon, cx, cy):
            continue
        out.setdefault(int(d["frame_idx"]), []).append((cx, cy))
    return out


def ball_px_at(cy: float, rows, sizes) -> float:
    """Estimated source ball diameter (px) at source row cy, from the gradient."""
    rows = np.asarray(rows, float)
    sizes = np.asarray(sizes, float)
    order = np.argsort(rows)
    return float(np.interp(cy, rows[order], sizes[order]))


def warped_box(
    warp, cx: float, cy: float, src_px: float
) -> tuple[float, float, float, float]:
    """Warp a source ball box's corners → warped (xc, yc, w, h) in warped px.

    Warping the corners (not just the center) makes an anisotropic warp's
    elliptical near-ball get a correct wide box.
    """
    r = max(src_px, 4.0) / 2.0
    corners = np.array(
        [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]], float
    )
    w = warp.points(corners)
    x0, y0 = w[:, 0].min(), w[:, 1].min()
    x1, y1 = w[:, 0].max(), w[:, 1].max()
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0, max(x1 - x0, 2.0), max(y1 - y0, 2.0)


# ---------------------------------------------------------------------------
# Dataset generation
# ---------------------------------------------------------------------------


@dataclass
class GameSpec:
    game_id: str
    video_path: str
    polygon: list  # field-outline polygon, source coords
    labels: dict  # {frame_idx: [(cx,cy), ...]} in source coords
    camera: str  # 'reolink' | 'dahua'


def generate_yolo_dataset(
    games: list[GameSpec],
    warp_kind: str,
    target_width: int,
    out_dir,
    *,
    rows=None,
    sizes=None,
    val_game_ids: set[str] | None = None,
    max_frames_per_game: int | None = None,
    pad_to_stride: bool = True,
) -> dict:
    """Warp labeled frames + write a standard YOLO dataset (images/labels/yaml).

    Returns a summary dict. Frames go to train/ unless their game is in
    val_game_ids. Ball boxes are warped through the same transform as the frame.
    """
    import json
    from pathlib import Path

    import av
    import cv2

    if rows is None or sizes is None:
        rows, sizes = DEFAULT_REOLINK_GRADIENT

    out_dir = Path(out_dir)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    n_img = {"train": 0, "val": 0}
    n_lbl = 0
    out_shape = None

    for g in games:
        warp = make_warp(
            warp_kind,
            rows,
            sizes,
            _probe_wh(g.video_path)[0],
            _probe_wh(g.video_path)[1],
            target_width,
        )
        # crop_iso uses the polygon band, not the gradient rows, for y_top/y_bot:
        if warp_kind == "crop_iso":
            yt, yb = field_band_from_polygon(g.polygon)
            warp = CropIsoWarp(warp.src_w, warp.src_h, yt, yb, target_width)
        oh, ow = warp.shape
        if pad_to_stride:
            oh = _pad_to_stride(oh)
            ow = _pad_to_stride(ow)
        split = "val" if (val_game_ids and g.game_id in val_game_ids) else "train"

        container = av.open(str(g.video_path))
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        written = 0
        for idx, frame in enumerate(container.decode(stream)):
            if idx not in g.labels:
                continue
            img = frame.to_ndarray(format="bgr24")
            wimg = warp.frame(img)
            if pad_to_stride and wimg.shape[:2] != (oh, ow):
                canvas = np.zeros((oh, ow, 3), np.uint8)
                canvas[: wimg.shape[0], : wimg.shape[1]] = wimg
                wimg = canvas
            stem = f"{g.game_id}_f{idx:06d}"
            cv2.imwrite(str(out_dir / f"images/{split}/{stem}.jpg"), wimg)
            lines = []
            for cx, cy in g.labels[idx]:
                src_px = ball_px_at(cy, rows, sizes)
                bx, by, bw, bh = warped_box(warp, cx, cy, src_px)
                if not (0 <= bx < ow and 0 <= by < oh):
                    continue
                lines.append(
                    f"0 {bx / ow:.6f} {by / oh:.6f} {bw / ow:.6f} {bh / oh:.6f}"
                )
            (out_dir / f"labels/{split}/{stem}.txt").write_text("\n".join(lines))
            n_img[split] += 1
            n_lbl += len(lines)
            written += 1
            out_shape = (oh, ow)
            if max_frames_per_game and written >= max_frames_per_game:
                break
        container.close()

    yaml = (
        f"path: {out_dir}\ntrain: images/train\nval: images/val\n\nnc: 1\nnames: ['ball']\n"
        f"# v4 warp={warp_kind} target_width={target_width} frame_shape={out_shape}\n"
    )
    (out_dir / "dataset.yaml").write_text(yaml)
    summary = {
        "warp": warp_kind,
        "target_width": target_width,
        "frame_shape": out_shape,
        "images": n_img,
        "labels": n_lbl,
        "dataset_yaml": str(out_dir / "dataset.yaml"),
    }
    (out_dir / "generate_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _probe_wh(video_path: str) -> tuple[int, int]:
    import av

    c = av.open(str(video_path))
    try:
        s = c.streams.video[0]
        return int(s.codec_context.width), int(s.codec_context.height)
    finally:
        c.close()
