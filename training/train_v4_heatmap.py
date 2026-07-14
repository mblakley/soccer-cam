"""v4 ball detector — heatmap + multi-frame training entry.

Assembles training games from the far-label sets (the human far-ball
corrections + trusted reference near/mid detections), renders dewarped +
polygon-masked 3-frame crops with Gaussian center targets
(:mod:`training.data_prep.heatmap_dataset`), trains the compact U-Net
(:class:`training.models.heatmap_net.HeatmapNet`), and evaluates **center
distance** (the metric AutoCam was scored on: a hit = predicted peak within
``R`` px of the labelled ball — no IoU).

Train on the 05-27 sets; hold out the Irondequoit clip for eval vs AutoCam's
74% far-recall. Run:

    python -m training.train_v4_heatmap --out G:/v4bench/hm_ds --epochs 40
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# 05-27 sets share the (human-corrected) autocam-keypoint polygon; Irondequoit
# has its own. Override via --polygon-* if these move.
DEFAULT_POLY_0527 = "D:/detect_work/field_polygon_autocam.json"
DEFAULT_POLY_IRON = "D:/detect_work/v4_test_clips/irondequoit_field_polygon.json"

# The held-out eval set — NEVER allowed into training. Any far-label set whose
# name/clip is Spencerport is the evaluation ground truth (spc_normal* = NORMAL
# split, spc_clip*/spc_diverge = HARD far split); training on it would leak the
# eval and silently inflate every recall number. Enforced by assert below.
# Iron 06.15 is ALSO held-out: `heat_0615_*` and `iron_ourloss_spans` are both the
# 2026.06.15-vs-Irondequoit game — the clip token `2026.06.15` catches them via the
# manifest clip path (06.04 Irondequoit stays trainable), `0615` catches the set names.
HELD_OUT_TOKENS = ("spc", "spencerport", "2026.06.15", "0615")

# Per-far-label-set field polygons keyed by a token in the set's dir name. A set
# whose name contains one of these keys uses the mapped human-verified polygon;
# anything else falls back to DEFAULT_POLY_0527. This is the generic "wire a new
# game in as a training set" hook — add a (token, polygon_path) pair to onboard a
# game. (``heat_0615_*`` was mapped here, but 6/15 Irondequoit is HELD-OUT eval —
# see HELD_OUT_TOKENS — so those sets are excluded before this map is consulted.)
SET_POLYGONS = {
    "irondequoit": DEFAULT_POLY_IRON,
}


def _is_held_out(name: str, clip: str = "") -> bool:
    n = name.lower()
    c = (clip or "").lower()
    return any(t in n or t in c for t in HELD_OUT_TOKENS)


def _ball_labels(labels_path: Path) -> dict[int, tuple[float, float]]:
    if not labels_path.exists():
        return {}
    out = {}
    for label in json.loads(labels_path.read_text()):
        if label.get("action") == "ball" and label.get("x") is not None:
            out[int(label["frame_idx"])] = (float(label["x"]), float(label["y"]))
    return out


def _set_polygon(name: str) -> list:
    n = name.lower()
    for key, path in SET_POLYGONS.items():
        if key in n:
            return json.loads(Path(path).read_text())["polygon"]
    return json.loads(Path(DEFAULT_POLY_0527).read_text())["polygon"]


def assemble_games(
    far_label_dir: str, include_sets: list[str] | None = None
) -> list[dict]:
    """Build game specs from the far-label sets.

    Each set directory becomes a training game: its human ``labels.json`` ball
    clicks (global frame_idx on the set's raw ``clip``) are the labels, and the
    set's field polygon comes from :data:`SET_POLYGONS`. ``include_sets`` (set-dir
    names) restricts to those sets; ``None`` = every set with labels. The held-out
    Spencerport eval is ALWAYS excluded from training (hard assert) — it is the GT.
    """
    root = Path(far_label_dir)
    want = set(include_sets) if include_sets else None
    games = []
    for d in sorted(root.iterdir()):
        man = d / "manifest.json"
        if not man.exists():
            continue
        if want is not None and d.name not in want:
            continue
        m = json.loads(man.read_text())
        if _is_held_out(d.name, m.get("clip", "")):
            # CRITICAL eval-leak guard: spc_normal1 (+ any Spencerport set) is the
            # held-out evaluation GT and must never be trained on.
            continue
        labels = _ball_labels(d / "labels.json")
        if not labels:
            continue
        games.append(
            {
                "game_id": d.name,
                "video": m["clip"],
                "polygon": _set_polygon(d.name),
                "labels": labels,
                "split": "train",
            }
        )
    assert not any(_is_held_out(g["game_id"], g["video"]) for g in games), (
        "held-out set leaked into training games (Spencerport 05.31 / Iron 06.15)"
    )
    return games


def center_distance_eval(model, ds, device, radius=20, thr=0.5):
    """Recall/precision by peak-vs-target center distance on a crop dataset."""
    import torch

    from training.models.heatmap_net import peak_xy

    model.eval()
    hit = miss = fp = 0
    with torch.no_grad():
        for i in range(len(ds)):
            x, tgt = ds[i]
            logits = model(x[None].to(device))[0, 0]
            px, py, score = peak_xy(logits)
            has_ball = float(tgt.max()) > 0.5
            if has_ball:
                flat = int(torch.argmax(tgt[0]))
                ty, tx = divmod(flat, tgt.shape[-1])
                ok = score >= thr and ((px - tx) ** 2 + (py - ty) ** 2) ** 0.5 <= radius
                hit += ok
                miss += not ok
            elif score >= thr:
                fp += 1
    n = hit + miss
    return {
        "recall": round(hit / n, 3) if n else 0.0,
        "hit": hit,
        "n": n,
        "fp_on_bg": fp,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--far-label-dir", default="D:/training_data/far_label")
    ap.add_argument("--out", default="G:/v4bench/hm_ds")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help="DataLoader workers; raise to feed a data-starved GPU (was hardcoded 4)",
    )
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--sigma", type=float, default=4.0)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--rebuild", action="store_true", help="re-render crops")
    ap.add_argument(
        "--far-weight",
        type=float,
        default=0.0,
        help=(
            "Far-band positive-loss up-weighting strength K (default 0 = OFF = the "
            "current uniform w=1+30*tgt EXACTLY). When K>0, a positive crop's ball "
            "pixels are additionally scaled by (1 + K*(1-depth)), where depth in "
            "[0,1] is the ball's normalized field depth (0=far touchline, 1=near). "
            "So a far ball weighs up to (1+K)x a near ball, pushing the loss to make "
            "the far ball the strongest peak. Requires a crop store built with "
            "record_depth=True (auto-enabled here when K>0); background/negative "
            "crops are unaffected. K=0 leaves training byte-identical to the curve."
        ),
    )
    ap.add_argument(
        "--base",
        type=int,
        default=24,
        help="HeatmapNet base channel width (24 = the distill baseline capacity).",
    )
    ap.add_argument(
        "--runs",
        default=None,
        help="checkpoint dir (default <out>/runs). Set per experiment to avoid clobbering.",
    )
    ap.add_argument(
        "--resume",
        default=None,
        help="warm-start model weights from this checkpoint (fine-tune, e.g. a hard-neg pass "
        "on top of best.pt) instead of training from scratch.",
    )
    ap.add_argument(
        "--dynamic-sigma",
        action="store_true",
        help="Experiment A (dynamic ball size): per-sample target gaussian sigma scales with the "
        "ball's field DEPTH to match the real perspective size gradient. CropIsoWarp preserves "
        "perspective (see EXP-DIST-49 correction), so ball apparent size runs ~4px (far) -> ~17px "
        "(near) — fixed sigma=4 fits near but is ~4x too broad for far. Suggested --sigma-far ~1.5, "
        "--sigma-near ~5. Records depth (like --far-weight); train from scratch.",
    )
    ap.add_argument(
        "--sigma-far",
        type=float,
        default=1.5,
        help="target sigma at depth=0 (far touchline, ~4px ball) when --dynamic-sigma",
    )
    ap.add_argument(
        "--sigma-near",
        type=float,
        default=5.0,
        help="target sigma at depth=1 (near touchline, ~17px ball) when --dynamic-sigma",
    )
    args = ap.parse_args()

    import torch
    from torch.utils.data import DataLoader

    from training.data_prep.heatmap_dataset import (
        HeatmapCropDataset,
        build_heatmap_crops,
        gaussian_heatmap,
    )
    from training.models.heatmap_net import HeatmapNet

    far_on = args.far_weight > 0.0
    dyn_on = args.dynamic_sigma

    out = Path(args.out)
    if args.rebuild or not (out / "index.json").exists():
        games = assemble_games(args.far_label_dir)
        val_ids = {g["game_id"] for g in games if g["split"] == "val"}
        print(f"assembled {len(games)} games; val={val_ids}", flush=True)
        summary = build_heatmap_crops(
            games,
            out,
            crop=args.crop,
            sigma=args.sigma,
            val_game_ids=val_ids,
            record_depth=far_on or dyn_on,
        )
        print("dataset:", summary, flush=True)

    class _DepthDataset(HeatmapCropDataset):
        """Adds per-sample field depth (positives) so the loss can far-band weight.

        Returns ``(stack, tgt, far_factor)`` where ``far_factor = 1-depth`` for
        positives (1 at the far touchline, 0 near) and 0 for negatives. Only used
        when ``--far-weight > 0``; the default path uses the unchanged
        ``HeatmapCropDataset`` (byte-identical to the curve)."""

        def __getitem__(self, i):
            import random

            import numpy as _np

            r = self.items[i]
            stack = (
                _np.load(self.root / "crops" / r["file"]).astype(_np.float32) / 255.0
            )
            if r["x"] is None:
                tgt = _np.zeros((self.crop, self.crop), _np.float32)
                far = 0.0
            else:
                tgt = gaussian_heatmap(self.crop, self.crop, r["x"], r["y"], self.sigma)
                far = float(1.0 - r.get("depth", 0.0))  # far touchline -> 1.0
            if self.augment and random.random() < 0.5:
                stack = _np.ascontiguousarray(stack[:, :, ::-1])
                tgt = _np.ascontiguousarray(tgt[:, ::-1])
            return (
                torch.from_numpy(stack),
                torch.from_numpy(tgt[None]),
                torch.tensor(far, dtype=torch.float32),
            )

    class _DynSigmaDataset(HeatmapCropDataset):
        """Experiment A: per-sample target sigma = sigma_far + (sigma_near - sigma_far) * depth
        (depth 0=far -> tight gaussian; 1=near -> broad) so the target encodes the ball's
        location-dependent apparent size. Negatives stay all-zero. Returns (stack, tgt)."""

        sigma_far: float = 2.0
        sigma_near: float = 8.0

        def __getitem__(self, i):
            import random

            import numpy as _np

            r = self.items[i]
            stack = (
                _np.load(self.root / "crops" / r["file"]).astype(_np.float32) / 255.0
            )
            if r["x"] is None:
                tgt = _np.zeros((self.crop, self.crop), _np.float32)
            else:
                depth = float(r.get("depth", 0.5))
                sig = self.sigma_far + (self.sigma_near - self.sigma_far) * depth
                tgt = gaussian_heatmap(self.crop, self.crop, r["x"], r["y"], sig)
            if self.augment and random.random() < 0.5:
                stack = _np.ascontiguousarray(stack[:, :, ::-1])
                tgt = _np.ascontiguousarray(tgt[:, ::-1])
            return torch.from_numpy(stack), torch.from_numpy(tgt[None])

    if dyn_on:
        ds_cls: type = _DynSigmaDataset
    elif far_on:
        ds_cls = _DepthDataset
    else:
        ds_cls = HeatmapCropDataset
    tr = ds_cls(out, "train", args.crop, args.sigma)
    if dyn_on:
        tr.sigma_far = args.sigma_far
        tr.sigma_near = args.sigma_near
    va = HeatmapCropDataset(out, "val", args.crop, args.sigma)
    print(
        f"train crops={len(tr)} val crops={len(va)} far_weight={args.far_weight}",
        flush=True,
    )
    if len(tr) == 0:
        raise SystemExit("no training crops — label some far balls first")

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    dl = DataLoader(
        tr,
        batch_size=args.batch,
        shuffle=True,
        num_workers=args.workers,
        drop_last=True,
        persistent_workers=args.workers > 0,
        prefetch_factor=4 if args.workers > 0 else None,
        pin_memory=True,
    )
    model = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=args.base).to(dev)
    if args.resume:
        rck = torch.load(args.resume, map_location=dev)
        model.load_state_dict(rck["model"] if "model" in rck else rck)
        print(f"resumed (warm-start) from {args.resume}", flush=True)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    runs = Path(args.runs) if args.runs else (out / "runs")
    runs.mkdir(parents=True, exist_ok=True)
    best = -1.0
    for ep in range(args.epochs):
        model.train()
        tot = 0.0
        for batch in dl:
            if far_on:
                x, tgt, far = batch
                far = far.to(dev).view(-1, 1, 1, 1)
            else:
                x, tgt = batch
            x, tgt = x.to(dev), tgt.to(dev)
            pred = torch.sigmoid(model(x))
            # emphasize the (rare) ball pixels: weighted MSE.
            w = 1.0 + 30.0 * tgt
            if far_on:
                # Far-band up-weighting: scale ONLY the positive (ball-pixel) weight
                # by (1 + K*far_factor) so far balls dominate the loss. tgt acts as
                # the positive mask, so background weight (the leading 1.0) is
                # untouched. K=0 collapses this to exactly w=1+30*tgt.
                w = 1.0 + 30.0 * tgt * (1.0 + args.far_weight * far)
            loss = (w * (pred - tgt) ** 2).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss)
        metrics = center_distance_eval(model, va, dev) if len(va) else {"recall": 0.0}
        print(
            f"epoch {ep + 1}/{args.epochs} loss={tot / max(len(dl), 1):.4f} "
            f"val={metrics}",
            flush=True,
        )
        if metrics["recall"] >= best:
            best = metrics["recall"]
            torch.save({"model": model.state_dict(), "epoch": ep}, runs / "best.pt")
    print(f"DONE best_val_recall={best}", flush=True)


if __name__ == "__main__":
    main()
