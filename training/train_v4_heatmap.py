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


def _ball_labels(labels_path: Path) -> dict[int, tuple[float, float]]:
    if not labels_path.exists():
        return {}
    out = {}
    for label in json.loads(labels_path.read_text()):
        if label.get("action") == "ball" and label.get("x") is not None:
            out[int(label["frame_idx"])] = (float(label["x"]), float(label["y"]))
    return out


def assemble_games(far_label_dir: str) -> list[dict]:
    """Build game specs from the far-label sets (heat_0527_* train, irondequoit val)."""
    root = Path(far_label_dir)
    games = []
    for d in sorted(root.iterdir()):
        man = d / "manifest.json"
        if not man.exists():
            continue
        m = json.loads(man.read_text())
        labels = _ball_labels(d / "labels.json")
        if not labels:
            continue
        is_iron = "irondequoit" in d.name
        poly_path = DEFAULT_POLY_IRON if is_iron else DEFAULT_POLY_0527
        games.append(
            {
                "game_id": d.name,
                "video": m["clip"],
                "polygon": json.loads(Path(poly_path).read_text())["polygon"],
                "labels": labels,
                "split": "val" if is_iron else "train",
            }
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
                ty, tx = [int(v) for v in torch.where(tgt[0] == tgt[0].max())]
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
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--sigma", type=float, default=4.0)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--rebuild", action="store_true", help="re-render crops")
    args = ap.parse_args()

    import torch
    from torch.utils.data import DataLoader

    from training.data_prep.heatmap_dataset import (
        HeatmapCropDataset,
        build_heatmap_crops,
    )
    from training.models.heatmap_net import HeatmapNet

    out = Path(args.out)
    if args.rebuild or not (out / "index.json").exists():
        games = assemble_games(args.far_label_dir)
        val_ids = {g["game_id"] for g in games if g["split"] == "val"}
        print(f"assembled {len(games)} games; val={val_ids}", flush=True)
        summary = build_heatmap_crops(
            games, out, crop=args.crop, sigma=args.sigma, val_game_ids=val_ids
        )
        print("dataset:", summary, flush=True)

    tr = HeatmapCropDataset(out, "train", args.crop, args.sigma)
    va = HeatmapCropDataset(out, "val", args.crop, args.sigma)
    print(f"train crops={len(tr)} val crops={len(va)}", flush=True)
    if len(tr) == 0:
        raise SystemExit("no training crops — label some far balls first")

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    dl = DataLoader(
        tr, batch_size=args.batch, shuffle=True, num_workers=4, drop_last=True
    )
    model = HeatmapNet(in_frames=3, in_ch_per_frame=1).to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    runs = Path("G:/v4bench/runs/hm_v4")
    runs.mkdir(parents=True, exist_ok=True)
    best = -1.0
    for ep in range(args.epochs):
        model.train()
        tot = 0.0
        for x, tgt in dl:
            x, tgt = x.to(dev), tgt.to(dev)
            pred = torch.sigmoid(model(x))
            # emphasize the (rare) ball pixels: weighted MSE
            w = 1.0 + 30.0 * tgt
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
