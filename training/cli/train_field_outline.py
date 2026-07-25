"""Train the student field-outline model on distilled teacher labels.

Fits :class:`~training.field_outline.model.FieldOutlineNet` to the
10-point labels written by ``generate_field_outline_labels``. Splits are by venue
placement (never by frame); the coordinate loss is supervised only on
in-frame, confident points of frames the teacher itself trusted, while the
score head distills the teacher's per-point confidence.

Run on the GPU server (the footage and CUDA live there). Examples::

    # Overfit smoke test (proves model/loss plumbing) on a few frames
    python -m training.cli.train_field_outline \\
        --dataset-root F:/training_data/field_outline \\
        --limit-frames 64 --no-aug --epochs 50 --device cpu

    # Real run
    python -m training.cli.train_field_outline \\
        --dataset-root F:/training_data/field_outline \\
        --epochs 100 --batch 32
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, WeightedRandomSampler

from training.field_outline import INPUT_H, INPUT_W
from training.field_outline.dataset import (
    build_datasets,
    cluster_weights,
    polygon_iou,
)
from training.field_outline.model import FieldOutlineNet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Losses + metrics (pure tensor ops — unit-tested)
# ---------------------------------------------------------------------------


def coord_loss(
    pred_kpts: torch.Tensor,
    tgt_kpts: torch.Tensor,
    scores: torch.Tensor,
    coord_valid: torch.Tensor,
    beta: float = 0.01,
) -> torch.Tensor:
    """Score-weighted Smooth-L1 over valid points (normalized coords)."""
    per_pt = F.smooth_l1_loss(pred_kpts, tgt_kpts, beta=beta, reduction="none").sum(-1)
    weight = coord_valid * scores
    return (per_pt * weight).sum() / weight.sum().clamp_min(1.0)


def score_loss(pred_scores: torch.Tensor, tgt_scores: torch.Tensor) -> torch.Tensor:
    """Soft-target BCE distilling the teacher's per-point confidence."""
    return F.binary_cross_entropy(pred_scores, tgt_scores.clamp(0.0, 1.0))


def pixel_error(
    pred_kpts: torch.Tensor, tgt_kpts: torch.Tensor, coord_valid: torch.Tensor
) -> torch.Tensor:
    """Mean per-point Euclidean error in 768x384 pixel space over valid points."""
    scale = torch.tensor([INPUT_W, INPUT_H], device=pred_kpts.device)
    dist = ((pred_kpts - tgt_kpts) * scale).norm(dim=-1)
    return (dist * coord_valid).sum() / coord_valid.sum().clamp_min(1.0)


# ---------------------------------------------------------------------------
# Train / eval loops
# ---------------------------------------------------------------------------


def _run_epoch(model, loader, device, optimizer=None, score_weight=0.25):
    """One epoch. Train if ``optimizer`` given, else evaluate. Returns stats."""
    train = optimizer is not None
    model.train(train)
    tot_loss = tot_coord = tot_score = 0.0
    px_sum = px_n = 0.0
    iou_sum = iou_n = 0
    n_batches = 0

    for batch in loader:
        image = batch["image"].to(device)
        kpts = batch["kpts"].to(device)
        scores = batch["scores"].to(device)
        valid = batch["coord_valid"].to(device)

        with torch.set_grad_enabled(train):
            pred_k, pred_s = model(image)
            lc = coord_loss(pred_k, kpts, scores, valid)
            ls = score_loss(pred_s, scores)
            loss = lc + score_weight * ls
            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

        tot_loss += float(loss)
        tot_coord += float(lc)
        tot_score += float(ls)
        n_batches += 1

        with torch.no_grad():
            if valid.sum() > 0:
                px_sum += float(pixel_error(pred_k, kpts, valid)) * float(valid.sum())
                px_n += float(valid.sum())
            pk = pred_k.detach().cpu().numpy()
            tk = kpts.detach().cpu().numpy()
            for i in range(pk.shape[0]):
                iou_sum += polygon_iou(pk[i], tk[i])
                iou_n += 1

    nb = max(n_batches, 1)
    return {
        "loss": tot_loss / nb,
        "coord": tot_coord / nb,
        "score": tot_score / nb,
        "px_err": px_sum / px_n if px_n else float("nan"),
        "iou": iou_sum / iou_n if iou_n else float("nan"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train field-outline student")
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument(
        "--backbone", default="resnet18", choices=["resnet18", "mobilenet_v3_small"]
    )
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--lr-head", type=float, default=1e-3)
    parser.add_argument("--lr-backbone", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-2)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--score-weight", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument(
        "--device", default="cuda" if torch.cuda.is_available() else "cpu"
    )
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--no-pretrained", action="store_true")
    parser.add_argument(
        "--limit-frames", type=int, default=0, help="smoke: cap train frames"
    )
    parser.add_argument(
        "--no-aug", action="store_true", help="smoke: disable augmentation"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    train_ds, val_ds, _test_ds, split = build_datasets(args.dataset_root, args.seed)

    if args.no_aug:
        train_ds.train = False
    if args.limit_frames:
        train_ds.samples = train_ds.samples[: args.limit_frames]

    # Data loading is the bottleneck (GPU was 0% util, starved): keep workers
    # alive across epochs (Windows re-spawn is very expensive), pin memory, and
    # prefetch so the GPU isn't waiting on JPEG decode.
    loader_kw = {
        "num_workers": args.num_workers,
        "pin_memory": True,
        "persistent_workers": args.num_workers > 0,
        "prefetch_factor": 4 if args.num_workers > 0 else None,
    }
    if args.limit_frames:
        train_loader = DataLoader(
            train_ds, batch_size=args.batch, shuffle=True, **loader_kw
        )
    else:
        weights = cluster_weights(train_ds.samples)
        sampler = WeightedRandomSampler(
            weights, num_samples=len(train_ds), replacement=True
        )
        train_loader = DataLoader(
            train_ds, batch_size=args.batch, sampler=sampler, **loader_kw
        )
    val_loader = (
        DataLoader(val_ds, batch_size=args.batch, **loader_kw) if len(val_ds) else None
    )

    device = torch.device(args.device)
    model = FieldOutlineNet(args.backbone, pretrained=not args.no_pretrained).to(device)

    optimizer = torch.optim.AdamW(
        [
            {"params": model.backbone_parameters(), "lr": args.lr_backbone},
            {"params": model.head_parameters(), "lr": args.lr_head},
        ],
        weight_decay=args.weight_decay,
    )
    warmup = min(args.warmup, max(args.epochs - 1, 1))
    sched = torch.optim.lr_scheduler.SequentialLR(
        optimizer,
        [
            torch.optim.lr_scheduler.LinearLR(optimizer, 0.1, 1.0, total_iters=warmup),
            torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=max(args.epochs - warmup, 1), eta_min=1e-6
            ),
        ],
        milestones=[warmup],
    )

    run_name = args.run_name or time.strftime("field_outline_%Y%m%d_%H%M%S")
    run_dir = Path("training/runs") / run_name
    (run_dir / "weights").mkdir(parents=True, exist_ok=True)
    (run_dir / "args.json").write_text(
        json.dumps({**vars(args), "split": split}, indent=2, default=str)
    )
    csv_path = run_dir / "results.csv"
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerow(
            [
                "epoch",
                "train_loss",
                "train_coord",
                "train_score",
                "val_px_err",
                "val_iou",
                "lr",
            ]
        )

    logger.info(
        "Training %s on %s: train=%d val=%d",
        run_name,
        device,
        len(train_ds),
        len(val_ds),
    )
    best_metric = float("inf")
    best_epoch = -1
    since_improve = 0

    for epoch in range(1, args.epochs + 1):
        tr = _run_epoch(model, train_loader, device, optimizer, args.score_weight)
        if val_loader:
            va = _run_epoch(model, val_loader, device, None, args.score_weight)
            monitor = va["px_err"]
        else:
            va = {"px_err": float("nan"), "iou": float("nan")}
            monitor = tr["loss"]
        sched.step()
        lr = optimizer.param_groups[0]["lr"]

        with open(csv_path, "a", newline="") as f:
            csv.writer(f).writerow(
                [
                    epoch,
                    f"{tr['loss']:.5f}",
                    f"{tr['coord']:.5f}",
                    f"{tr['score']:.5f}",
                    f"{va['px_err']:.3f}",
                    f"{va['iou']:.4f}",
                    f"{lr:.2e}",
                ]
            )
        logger.info(
            "epoch %3d/%d  train_loss=%.4f coord=%.4f  val_px_err=%.2f val_iou=%.3f",
            epoch,
            args.epochs,
            tr["loss"],
            tr["coord"],
            va["px_err"],
            va["iou"],
        )

        ckpt = {
            "model": model.state_dict(),
            "backbone": args.backbone,
            "epoch": epoch,
            "val_px_err": va["px_err"],
            "val_iou": va["iou"],
        }
        torch.save(ckpt, run_dir / "weights" / "last.pt")
        if monitor < best_metric:
            best_metric, best_epoch, since_improve = monitor, epoch, 0
            torch.save(ckpt, run_dir / "weights" / "best.pt")
        else:
            since_improve += 1
            if since_improve >= args.patience:
                logger.info("Early stop at epoch %d (best=%d)", epoch, best_epoch)
                break

    logger.info(
        "Done. best epoch=%d metric=%.4f  weights=%s",
        best_epoch,
        best_metric,
        run_dir / "weights" / "best.pt",
    )


if __name__ == "__main__":
    main()
