"""Train a temporal (3-frame) heatmap model for ball detection.

Uses a lightweight U-Net encoder-decoder that takes 9-channel input
(3 consecutive RGB frames) and outputs a single-channel heatmap
predicting the ball center location.

Separate from train.py — different architecture, custom dataloader.
"""

import argparse
import logging
import time
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Architecture: Lightweight U-Net for heatmap regression
# ---------------------------------------------------------------------------


class ConvBlock(nn.Module):
    """Two 3x3 convolutions with BatchNorm and ReLU."""

    def __init__(self, in_ch: int, out_ch: int):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
        )

    def forward(self, x):
        return self.conv(x)


class TemporalBallNet(nn.Module):
    """Lightweight U-Net for temporal ball heatmap prediction.

    Input: (B, 9, H, W) — 3 consecutive RGB frames
    Output: (B, 1, H, W) — heatmap with Gaussian peak at ball center

    ~4M parameters. Designed for 640x640 input.
    """

    def __init__(self, in_channels: int = 9):
        super().__init__()

        # Encoder
        self.enc1 = ConvBlock(in_channels, 32)
        self.enc2 = ConvBlock(32, 64)
        self.enc3 = ConvBlock(64, 128)
        self.enc4 = ConvBlock(128, 256)

        self.pool = nn.MaxPool2d(2)

        # Bottleneck
        self.bottleneck = ConvBlock(256, 512)

        # Decoder
        self.up4 = nn.ConvTranspose2d(512, 256, 2, stride=2)
        self.dec4 = ConvBlock(512, 256)

        self.up3 = nn.ConvTranspose2d(256, 128, 2, stride=2)
        self.dec3 = ConvBlock(256, 128)

        self.up2 = nn.ConvTranspose2d(128, 64, 2, stride=2)
        self.dec2 = ConvBlock(128, 64)

        self.up1 = nn.ConvTranspose2d(64, 32, 2, stride=2)
        self.dec1 = ConvBlock(64, 32)

        # Output head
        self.out_conv = nn.Conv2d(32, 1, 1)

    def forward(self, x):
        # Encoder
        e1 = self.enc1(x)  # (B, 32, H, W)
        e2 = self.enc2(self.pool(e1))  # (B, 64, H/2, W/2)
        e3 = self.enc3(self.pool(e2))  # (B, 128, H/4, W/4)
        e4 = self.enc4(self.pool(e3))  # (B, 256, H/8, W/8)

        # Bottleneck
        b = self.bottleneck(self.pool(e4))  # (B, 512, H/16, W/16)

        # Decoder with skip connections
        d4 = self.dec4(torch.cat([self.up4(b), e4], dim=1))
        d3 = self.dec3(torch.cat([self.up3(d4), e3], dim=1))
        d2 = self.dec2(torch.cat([self.up2(d3), e2], dim=1))
        d1 = self.dec1(torch.cat([self.up1(d2), e1], dim=1))

        return torch.sigmoid(self.out_conv(d1))  # (B, 1, H, W)


# ---------------------------------------------------------------------------
# Loss: Weighted focal loss for heatmap regression
# ---------------------------------------------------------------------------


class WeightedFocalLoss(nn.Module):
    """Focal-style loss for heatmap regression.

    Heavily weights the rare ball pixels vs abundant background.
    Based on CornerNet loss adapted for Gaussian heatmaps.
    """

    def __init__(self, alpha: float = 2.0, beta: float = 4.0):
        super().__init__()
        self.alpha = alpha
        self.beta = beta

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """Compute focal loss.

        Args:
            pred: (B, 1, H, W) predicted heatmap in [0, 1]
            target: (B, 1, H, W) target heatmap in [0, 1]
        """
        # Clamp for numerical stability
        pred = pred.clamp(1e-6, 1 - 1e-6)

        # Positive locations (target == 1 peak)
        pos_mask = target.eq(1).float()
        neg_mask = target.lt(1).float()

        # Positive loss
        pos_loss = -((1 - pred) ** self.alpha) * torch.log(pred) * pos_mask

        # Negative loss (downweight based on distance from peak)
        neg_weight = (1 - target) ** self.beta
        neg_loss = -(pred**self.alpha) * torch.log(1 - pred) * neg_weight * neg_mask

        # Normalize by number of positive samples
        n_pos = pos_mask.sum()
        if n_pos == 0:
            loss = neg_loss.sum()
        else:
            loss = (pos_loss.sum() + neg_loss.sum()) / n_pos

        return loss


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------


def train_temporal(
    manifest_path: Path,
    output_dir: Path,
    epochs: int = 100,
    batch_size: int = 4,
    lr: float = 1e-3,
    device: str = "0",
    num_workers: int = 0,
    val_manifest: Path | None = None,
    resume: Path | None = None,
):
    """Train the temporal ball detection model.

    Args:
        manifest_path: Path to training triplet manifest (.jsonl)
        output_dir: Directory for checkpoints and logs
        epochs: Number of training epochs
        batch_size: Batch size (4 for 8GB VRAM, 8 for 12GB)
        lr: Learning rate
        device: CUDA device ("0") or "cpu"
        num_workers: DataLoader workers (0 for Windows/low RAM)
        val_manifest: Optional validation manifest path
        resume: Path to checkpoint to resume from
    """
    from training.temporal_dataset import TemporalBallDataset

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Device setup
    if device == "cpu":
        dev = torch.device("cpu")
    else:
        dev = torch.device(f"cuda:{device}")
    logger.info("Using device: %s", dev)

    # Dataset and dataloader
    train_dataset = TemporalBallDataset(manifest_path, augment=True)
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True,
    )

    val_loader = None
    if val_manifest and Path(val_manifest).exists():
        val_dataset = TemporalBallDataset(val_manifest, augment=False)
        val_loader = DataLoader(
            val_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
            pin_memory=True,
        )

    # Model, loss, optimizer
    model = TemporalBallNet(in_channels=9).to(dev)
    criterion = WeightedFocalLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    start_epoch = 0
    best_val_loss = float("inf")

    # Resume from checkpoint
    if resume and Path(resume).exists():
        ckpt = torch.load(resume, map_location=dev, weights_only=False)
        model.load_state_dict(ckpt["model"])
        optimizer.load_state_dict(ckpt["optimizer"])
        scheduler.load_state_dict(ckpt["scheduler"])
        start_epoch = ckpt["epoch"] + 1
        best_val_loss = ckpt.get("best_val_loss", float("inf"))
        logger.info("Resumed from epoch %d", start_epoch)

    param_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("Model parameters: %.2fM", param_count / 1e6)
    logger.info(
        "Training samples: %d, batches/epoch: %d", len(train_dataset), len(train_loader)
    )

    # Training loop
    for epoch in range(start_epoch, epochs):
        model.train()
        epoch_loss = 0.0
        epoch_start = time.time()

        for batch_idx, (images, heatmaps, _meta) in enumerate(train_loader):
            images = images.to(dev)
            heatmaps = heatmaps.to(dev)

            pred = model(images)
            loss = criterion(pred, heatmaps)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_loss += loss.item()

            if (batch_idx + 1) % 50 == 0:
                logger.info(
                    "  Epoch %d [%d/%d] loss=%.4f",
                    epoch,
                    batch_idx + 1,
                    len(train_loader),
                    loss.item(),
                )

        scheduler.step()
        avg_loss = epoch_loss / max(len(train_loader), 1)
        epoch_time = time.time() - epoch_start

        # Validation
        val_loss_str = ""
        if val_loader:
            model.eval()
            val_loss = 0.0
            with torch.no_grad():
                for images, heatmaps, _ in val_loader:
                    images = images.to(dev)
                    heatmaps = heatmaps.to(dev)
                    pred = model(images)
                    val_loss += criterion(pred, heatmaps).item()
            avg_val_loss = val_loss / max(len(val_loader), 1)
            val_loss_str = f" val_loss={avg_val_loss:.4f}"

            if avg_val_loss < best_val_loss:
                best_val_loss = avg_val_loss
                torch.save(model.state_dict(), output_dir / "best.pt")
                val_loss_str += " *best*"

        logger.info(
            "Epoch %d/%d: train_loss=%.4f%s lr=%.6f (%.0fs)",
            epoch,
            epochs,
            avg_loss,
            val_loss_str,
            scheduler.get_last_lr()[0],
            epoch_time,
        )

        # Save checkpoint every 10 epochs
        if (epoch + 1) % 10 == 0 or epoch == epochs - 1:
            ckpt_path = output_dir / f"epoch_{epoch:03d}.pt"
            torch.save(
                {
                    "epoch": epoch,
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "scheduler": scheduler.state_dict(),
                    "best_val_loss": best_val_loss,
                },
                ckpt_path,
            )
            logger.info("Saved checkpoint: %s", ckpt_path)

    # Save final model
    torch.save(model.state_dict(), output_dir / "last.pt")
    logger.info("Training complete. Best val loss: %.4f", best_val_loss)


def main():
    parser = argparse.ArgumentParser(
        description="Train temporal heatmap model for ball detection"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("F:/training_data/temporal_triplets.jsonl"),
        help="Training triplet manifest",
    )
    parser.add_argument(
        "--val-manifest",
        type=Path,
        default=None,
        help="Validation triplet manifest",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("training/runs/temporal_v1"),
        help="Output directory for checkpoints",
    )
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--device", default="0", help="CUDA device or 'cpu'")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument(
        "--resume", type=Path, default=None, help="Checkpoint to resume from"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    train_temporal(
        args.manifest,
        args.output,
        args.epochs,
        args.batch_size,
        args.lr,
        args.device,
        args.workers,
        args.val_manifest,
        args.resume,
    )


if __name__ == "__main__":
    main()
