"""Compact heatmap ball detector (v4).

A small, fully-convolutional U-Net that takes ``in_frames`` stacked grayscale
frames (temporal context — a moving ball pops against the static field) and
predicts a single ball-center **heatmap**. Peak-pick the heatmap → (x, y).

Why heatmap + multi-frame instead of bbox regression: the ball is 3–8 px, at or
below a bbox detector's stride, so IoU-based detection collapses; a per-pixel
center heatmap has no anchors/stride/IoU and a tiny ball is just a small Gaussian.
Fully convolutional, so it trains on small crops and runs on the whole field band
at inference. Tiny (~1–2 M params at ``base=16``) → CPU/edge inference, ONNX /
CoreML / TFLite exportable, comfortably within the 90-min-video-in-<24 h budget.

This is a standard small U-Net (no third-party model code).
"""

from __future__ import annotations

import torch
from torch import nn


class _DoubleConv(nn.Module):
    def __init__(self, c_in: int, c_out: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(c_in, c_out, 3, padding=1, bias=False),
            nn.BatchNorm2d(c_out),
            nn.ReLU(inplace=True),
            nn.Conv2d(c_out, c_out, 3, padding=1, bias=False),
            nn.BatchNorm2d(c_out),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class HeatmapNet(nn.Module):
    """U-Net: (B, in_frames*in_ch, H, W) -> (B, 1, H, W) heatmap logits.

    ``H`` and ``W`` must be multiples of 8 (three 2x downsamples). Output is the
    same spatial size as the input; apply ``sigmoid`` at loss/inference time.
    """

    def __init__(self, in_frames: int = 3, in_ch_per_frame: int = 1, base: int = 16):
        super().__init__()
        c = in_frames * in_ch_per_frame
        self.in_frames = in_frames
        self.in_ch_per_frame = in_ch_per_frame
        self.d1 = _DoubleConv(c, base)
        self.d2 = _DoubleConv(base, base * 2)
        self.d3 = _DoubleConv(base * 2, base * 4)
        self.d4 = _DoubleConv(base * 4, base * 8)
        self.pool = nn.MaxPool2d(2)
        self.up3 = nn.ConvTranspose2d(base * 8, base * 4, 2, stride=2)
        self.u3 = _DoubleConv(base * 8, base * 4)
        self.up2 = nn.ConvTranspose2d(base * 4, base * 2, 2, stride=2)
        self.u2 = _DoubleConv(base * 4, base * 2)
        self.up1 = nn.ConvTranspose2d(base * 2, base, 2, stride=2)
        self.u1 = _DoubleConv(base * 2, base)
        self.head = nn.Conv2d(base, 1, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x1 = self.d1(x)
        x2 = self.d2(self.pool(x1))
        x3 = self.d3(self.pool(x2))
        x4 = self.d4(self.pool(x3))
        y = self.u3(torch.cat([self.up3(x4), x3], 1))
        y = self.u2(torch.cat([self.up2(y), x2], 1))
        y = self.u1(torch.cat([self.up1(y), x1], 1))
        return self.head(y)  # logits


def peak_xy(heatmap: torch.Tensor) -> tuple[int, int, float]:
    """Argmax peak of a single ``(H, W)`` heatmap (logits or probs) -> (x, y, score).

    ``score`` is the sigmoid of the peak logit (so it's comparable in [0, 1]).
    """
    h, w = heatmap.shape[-2:]
    flat = heatmap.reshape(-1)
    i = int(torch.argmax(flat))
    y, x = divmod(i, w)
    score = float(torch.sigmoid(flat[i]))
    return x, y, score
