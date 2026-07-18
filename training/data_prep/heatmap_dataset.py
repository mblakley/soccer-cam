"""Heatmap training data for the v4 ball detector.

Pipeline per labeled frame (matches the runtime design: dewarp → polygon-mask →
heatmap):

1. **Dewarp** = crop the field band at **native resolution** (isotropic, so the
   ball stays round and keeps its native ~8 px size — no downscaling, which is
   what killed the bbox attempt).
2. **Polygon-mask** = zero every pixel outside the field polygon, with a generous
   **far margin** above the far touchline (far-corner balls sit slightly above the
   detected line; a tight crop would slice exactly the balls we care about). This
   removes the off-field false-positive sources (trees / netting / spectators).
3. **3 consecutive grayscale frames** ``[t-2, t-1, t]`` stacked → motion context
   (a moving ball pops against the static field; color is ~useless for the ball).
4. Fixed-size **crops** around the ball (+jitter) with a **Gaussian center target**,
   plus background crops as negatives. The net is fully convolutional, so training
   on crops and running on the whole masked band at inference is consistent.

Frames come from the raw per-segment clips via ``segment_decode`` (GOP=20 + VFR —
keyframe-seek + presentation-order-PTS matching is exact; EXP-DIST-21), so each
label costs ~one GOP of decode, corruption-isolated per segment.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from training.data_prep.segment_decode import iter_frames_from_segments
from training.data_prep.warped_dataset import (
    CropIsoWarp,  # noqa: F401  (re-export for existing callers)
    field_band_from_polygon,  # noqa: F401
    resolve_video_rotation,
)
from video_grouper.inference.iso_warp import (
    dewarp_mask_gray,
    far_margin_polygon,
    native_iso_warp,
)


def gaussian_heatmap(h: int, w: int, cx: float, cy: float, sigma: float) -> np.ndarray:
    """Single-blob Gaussian heatmap in [0,1], peak 1.0 at (cx, cy)."""
    ys, xs = np.ogrid[:h, :w]
    g = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2.0 * sigma * sigma))
    return g.astype(np.float32)


def person_center_heatmap(
    h: int,
    w: int,
    boxes: list,
    sigma_div: float = 8.0,
    sigma_min: float = 4.0,
    sigma_max: float = 12.0,
) -> np.ndarray:
    """Max-composited person-center Gaussians from ``[x1, y1, x2, y2, conf]`` boxes.

    σ scales with the person's box height (persons are 30–150 px, unlike the
    ball, so one fixed σ would mis-size most of them). Boxes partly outside the
    crop still contribute their in-crop mass.
    """
    hm = np.zeros((h, w), np.float32)
    for x1, y1, x2, y2, *_ in boxes:
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        sig = float(np.clip((y2 - y1) / sigma_div, sigma_min, sigma_max))
        np.maximum(hm, gaussian_heatmap(h, w, cx, cy, sig), out=hm)
    return hm


# The band/dewarp geometry is PRODUCT code now (video_grouper.inference.iso_warp,
# Mark 2026-07-10: single homegrown path). Underscore aliases kept for the
# existing training callers.


def _far_margin_polygon(polygon, far_margin: float) -> np.ndarray:
    return far_margin_polygon(polygon, far_margin)


def _native_iso_warp(polygon, src_w: int, src_h: int, target_width: int | None = None):
    return native_iso_warp(polygon, src_w, src_h, target_width)


def _dewarp_mask_gray(frame_bgr, warp, mask, stabilizer=None):
    return dewarp_mask_gray(frame_bgr, warp, mask, stabilizer)


def build_heatmap_crops(
    games: list[dict],
    out_dir,
    *,
    crop: int = 256,
    sigma: float = 4.0,
    jitter: int = 48,
    far_margin: float = 400.0,
    neg_ratio: float = 0.7,
    val_game_ids: set[str] | None = None,
    target_width: int | None = None,
    neg_per_pos: int = 1,
    hard_neg_crops: dict[str, list] | None = None,
    record_depth: bool = False,
    hwaccel: bool = False,
    stabilize: bool = False,
) -> dict:
    """Pre-render 3-frame grayscale crops + ball-center targets to ``out_dir``.

    Each ``games`` item: ``{game_id, video, polygon, labels: {frame_idx: (x, y)},
    split?}``. ``labels`` are SOURCE-pixel ball centers (human + trusted reference
    detections). Writes ``crops/*.npy`` (uint8 [3, crop, crop]) and an
    ``index.json`` with per-sample ``{file, x, y|null, split}`` (x/y in crop px;
    null = background/negative). Returns a summary.

    ``hard_neg_crops`` (default ``None``) maps ``"{game_id}|{frame_idx}"`` →
    a list of ``(x, y)`` band-coord distractor locations the model false-fired on;
    up to 2 per frame are emitted as extra **negative** crops, teaching it to
    suppress the actual players/lines it confused for the ball. ``None`` → no
    hard-negative crops (legacy behaviour).

    ``record_depth`` (default ``False`` → index byte-identical to the legacy
    schema) additionally records each POSITIVE sample's normalized **field depth**
    as ``"depth"`` ∈ [0, 1] (0 = far touchline / band top, 1 = near touchline /
    band bottom), derived from the ball's warped band-y. This is the per-sample
    signal a far-band loss up-weighting term consumes; it is purely additive
    metadata and never alters which crops are written, so a depth-recorded crop
    store trains identically to a legacy store under the default (uniform) loss.

    ``stabilize`` (default ``False`` → byte-identical legacy path) runs
    :class:`video_grouper.inference.iso_warp.BandStabilizer` wind alignment on
    every band before masking — anchor = the game's first decoded frame — and
    corrects each label's band coordinates by that frame's measured shift, so
    the 3-frame stack is jitter-free and the ball/mask geometry is registered
    to one reference (EXP-DIST-57: wind excursions put ~21% of windy-game ball
    positions outside the static mask). Recorded in the summary → a stabilized
    store gets its own index sha and can never masquerade as a legacy store.
    """
    import av
    import cv2

    from video_grouper.inference.iso_warp import BandStabilizer

    out_dir = Path(out_dir)
    (out_dir / "crops").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1234)
    index: list[dict] = []
    half = crop // 2

    for g in games:
        gid = g["game_id"]
        split = (
            "val" if (val_game_ids and gid in val_game_ids) else g.get("split", "train")
        )
        polygon = g["polygon"]
        labels = {int(k): v for k, v in g["labels"].items()}
        if not labels:
            continue
        # PyAV ignores the container's display-rotation; resolve it (explicit else game.json).
        # Rotation is applied INSIDE iter_frames_from_segments — frames arrive corrected.
        vrot = resolve_video_rotation(str(g["video"]), g.get("video_rotation"))
        with av.open(str(g["video"])) as _probe:
            _vs = _probe.streams.video[0]
            sw = _vs.codec_context.width
            sh = _vs.codec_context.height
        # Build BOTH the band crop and the mask from the far-margin-expanded polygon, so
        # the band top includes the far margin — airborne/very-far balls above the ground
        # far line stay in-band (cropping the band at the raw far line dropped ~1/3 of the
        # very-far GT balls, capping far recall).
        far_poly = _far_margin_polygon(polygon, far_margin)
        # per-game target_width (for cross-camera ball-size normalization) overrides the global one
        warp = _native_iso_warp(far_poly, sw, sh, g.get("target_width") or target_width)
        bh, bw = warp.shape
        mpoly = warp.points(far_poly).astype(np.int32)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [mpoly], 255)

        # Raw-segment streaming decode (EXP-DIST-21): pull ONLY each label's 3-frame
        # window from the raw per-segment clips — keyframe-seek + presentation-order
        # PTS matching (frame-exact, incl. on re-encoded clips), corruption-isolated
        # per segment. A non-combined basis (corrected/trimmed single clip) becomes
        # one synthetic segment: same PTS-exact path, local index == global index.
        video_p = Path(str(g["video"]))
        gjp = video_p.parent / "game.json"
        if video_p.name == "combined.mp4" and gjp.exists():
            segments = json.loads(gjp.read_text(encoding="utf-8", errors="ignore"))[
                "segments"
            ]
        else:
            segments = [{"seg": video_p.stem, "global_offset": 0, "frames": 10**9}]

        want = set(labels)
        grays: list = []
        dbx = dby = 0.0
        t = 0

        def _emit(ccx, ccy, has_ball, tag):
            x0 = int(np.clip(round(ccx) - half, 0, max(0, bw - crop)))
            y0 = int(np.clip(round(ccy) - half, 0, max(0, bh - crop)))
            stack = np.zeros((3, crop, crop), np.uint8)
            for i, gr in enumerate(grays):
                patch = gr[y0 : y0 + crop, x0 : x0 + crop]
                stack[i, : patch.shape[0], : patch.shape[1]] = patch
            if has_ball:
                lx, ly = dbx - x0, dby - y0
                if not (0 <= lx < crop and 0 <= ly < crop):
                    return
            else:
                lx = ly = None
            fname = f"{gid}_f{t:06d}_{tag}.npy"
            np.save(out_dir / "crops" / fname, stack)
            rec = {
                "file": fname,
                "x": None if lx is None else round(float(lx), 1),
                "y": None if ly is None else round(float(ly), 1),
                "split": split,
            }
            if record_depth and has_ball:
                # Normalized field depth from the ball's warped band-y: 0 = far
                # touchline (band top), 1 = near touchline (band bottom). bh is the
                # warped band height. Additive metadata only — never gates emission.
                rec["depth"] = round(float(np.clip(dby / max(bh, 1), 0.0, 1.0)), 4)
            index.append(rec)

        # Warp ONLY each label's 3-frame window [t-2, t-1, t]; the generator decodes
        # ~one GOP per cluster instead of walking the whole video.
        need: set[int] = set()
        for _t in want:
            need.update(k for k in (_t, _t - 1, _t - 2) if k >= 0)
        warped: dict[int, np.ndarray] = {}
        stab = BandStabilizer() if stabilize else None
        shifts: dict[int, tuple[float, float]] = {}
        for idx, img in iter_frames_from_segments(
            video_p.parent, segments, sorted(need), vrot, hwaccel=hwaccel
        ):
            warped[idx] = _dewarp_mask_gray(img, warp, mask, stab)
            if stab is not None:
                shifts[idx] = stab.last
            if idx in want:
                bx, by = labels[idx]
                seq = [warped.get(idx - 2), warped.get(idx - 1), warped.get(idx)]
                seq = [s for s in seq if s is not None]
                grays = seq if len(seq) == 3 else [seq[0]] * (3 - len(seq)) + seq
                for _k in [k for k in warped if k < idx - 2]:
                    del warped[_k]
                    shifts.pop(_k, None)
                dxy = warp.points([(bx, by)])[0]
                # the label lives on the RAW frame; pull it into aligned-band
                # coords by this frame's measured wind shift
                sdx, sdy = shifts.get(idx, (0.0, 0.0))
                dbx, dby = float(dxy[0]) - sdx, float(dxy[1]) - sdy
                t = idx
                jx = rng.integers(-jitter, jitter + 1)
                jy = rng.integers(-jitter, jitter + 1)
                _emit(dbx + jx, dby + jy, True, "pos")
                if rng.random() < neg_ratio:
                    # Emit several diverse in-field background negatives per positive.
                    # Full-frame search is dominated by background, so the model needs
                    # many negatives to learn ball-specific (not blob-like) responses.
                    made = 0
                    for _ in range(neg_per_pos * 12):
                        if made >= neg_per_pos:
                            break
                        nx = rng.integers(half, max(half + 1, bw - half))
                        ny = rng.integers(half, max(half + 1, bh - half))
                        if (
                            mask[int(ny), int(nx)]
                            and (nx - dbx) ** 2 + (ny - dby) ** 2 > (crop * 0.6) ** 2
                        ):
                            _emit(nx, ny, False, f"neg{made}")
                            made += 1
                # Hard negatives: the model's own false-fire locations (band coords)
                # for this (game, frame) — teaches it to suppress the actual
                # distractors (players/lines) it confused for the ball. Capped per
                # frame to stay near a stable neg:pos ratio.
                if hard_neg_crops:
                    for hi_, (hx, hy) in enumerate(
                        hard_neg_crops.get(f"{gid}|{idx}", [])[:2]
                    ):
                        _emit(float(hx), float(hy), False, f"hard{hi_}")

    n_train = sum(1 for r in index if r["split"] == "train")
    n_val = sum(1 for r in index if r["split"] == "val")
    summary = {
        "crop": crop,
        "sigma": sigma,
        "samples": len(index),
        "train": n_train,
        "val": n_val,
        "positives": sum(1 for r in index if r["x"] is not None),
    }
    if stabilize:
        # only when on — legacy stores stay byte-identical
        summary["stabilize"] = True
    (out_dir / "index.json").write_text(
        json.dumps({"summary": summary, "items": index})
    )
    from training.data_prep.store_versions import freeze_index

    v, sha = freeze_index(out_dir)
    print(f"STORE VERSIONED: created as v{v} ({sha})", flush=True)
    return summary


class HeatmapCropDataset:
    """torch Dataset over pre-rendered crops; builds the Gaussian target at load."""

    def __init__(
        self,
        root,
        split: str = "train",
        crop: int = 256,
        sigma: float | None = None,
        augment: bool | None = None,
        index_version: int | None = None,
    ):
        from training.data_prep.store_versions import resolve_index

        self.root = Path(root)
        # Provenance is pinned on EVERY construction (EXP-DIST-55: the store was
        # silently mutated between runs). None = freeze + use the current index;
        # an explicit version trains on that exact immutable snapshot.
        data, self.index_version, self.index_sha = resolve_index(
            self.root, index_version
        )
        self.crop = data.get("summary", {}).get("crop", crop)
        # σ precedence: an EXPLICIT sigma wins; None defers to the store summary
        # (the build-time σ). Targets are built at load time, so overriding σ on a
        # prebuilt store is valid — the old summary-always-wins rule silently turned
        # a `--sigma 3` run on a σ=4 store into an exact σ=4 replica.
        if sigma is None:
            self.sigma = data.get("summary", {}).get("sigma", 4.0)
        else:
            self.sigma = float(sigma)
        self.items = [r for r in data["items"] if r["split"] == split]
        # Horizontal-flip augmentation (train only): a mirrored field strip is a valid, different
        # soccer scene, so it adds real variety — cheaper diversity than pure oversampling.
        self.augment = (split == "train") if augment is None else augment

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        import random

        import torch

        r = self.items[i]
        stack = np.load(self.root / "crops" / r["file"]).astype(np.float32) / 255.0
        if r["x"] is None:
            tgt = np.zeros((self.crop, self.crop), np.float32)
        else:
            tgt = gaussian_heatmap(self.crop, self.crop, r["x"], r["y"], self.sigma)
        if self.augment and random.random() < 0.5:
            # mirror the band AND the target so the Gaussian peak lands at W-1-x
            stack = np.ascontiguousarray(stack[:, :, ::-1])
            tgt = np.ascontiguousarray(tgt[:, ::-1])
        return torch.from_numpy(stack), torch.from_numpy(tgt[None])
