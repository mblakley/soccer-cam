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
    """U-Net: (B, in_frames*in_ch, H, W) -> (B, out_ch, H, W) heatmap logits.

    ``H`` and ``W`` must be multiples of 8 (three 2x downsamples). Output is the
    same spatial size as the input; apply ``sigmoid`` at loss/inference time.

    ``out_ch`` is the number of center-heatmap channels (default 1 = ball only).
    Set ``out_ch=2`` for a **multi-task ball+person head** on the shared backbone:
    channel 0 = ball, channel 1 = person. Persons are supervised with their own
    center-heatmap (bootstrapped offline from a pretrained person detector), so the
    net learns ball-vs-person *appearance* on shared features — the ball head stops
    firing on a player's body even when the ball is right on it (EXP-11: box masking
    can't do this because it deletes the ball whenever it overlaps a player). No
    bbox/stride, so persons (big) and the 3-8px ball (tiny) share one cheap net.
    """

    def __init__(
        self,
        in_frames: int = 3,
        in_ch_per_frame: int = 1,
        base: int = 16,
        out_ch: int = 1,
    ):
        super().__init__()
        c = in_frames * in_ch_per_frame
        self.in_frames = in_frames
        self.in_ch_per_frame = in_ch_per_frame
        self.out_ch = out_ch
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
        self.head = nn.Conv2d(base, out_ch, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x1 = self.d1(x)
        x2 = self.d2(self.pool(x1))
        x3 = self.d3(self.pool(x2))
        x4 = self.d4(self.pool(x3))
        y = self.u3(torch.cat([self.up3(x4), x3], 1))
        y = self.u2(torch.cat([self.up2(y), x2], 1))
        y = self.u1(torch.cat([self.up1(y), x1], 1))
        return self.head(y)  # logits


class EncodingPrelude(nn.Module):
    """Parameter-free input encoding applied IN FRONT of the net (and baked into
    the ONNX graph at export), so the external input contract stays raw stacked
    grayscale frames ``[g_{t-2}, g_{t-1}, g_t]`` everywhere — training, eval
    CLIs, and the product runtime never diverge on the encoding math.

    Encodings:
      - ``gray3``: identity — the legacy contract (3 raw gray frames).
      - ``diff3``: ``(g_t, g_{t-1}-g_{t-2}, g_t-g_{t-1})`` — SIGNED frame
        differences replacing the two redundant history frames. A ball moving
        farther than its own diameter per frame leaves a direction-preserving
        ± lobe pair at its CURRENT position in the diff channels — a pixel-level
        motion signature no receptive field has to assemble. Signed (not |Δ|,
        which is polarity-blind and failed as an additive 4th channel — Jmot),
        and REPLACING the raw history so the temporal signal is load-bearing.
        Appearance stays frame t: labels and runtime candidates are keyed to t.
      - ``diff5``: ``(g_{t-2}, g_{t-1}, g_t, g_{t-1}-g_{t-2}, g_t-g_{t-1})`` —
        keep ALL gray frames, ADD the signed diffs (Mark's ablation of the
        EXP-DIST-51 result: diff3's "the raw frames are redundant" was an
        assumption — 3 frames at 0.07 bpp plausibly give implicit temporal
        denoising that replacement deleted, and diff channels are ~zero for
        slow balls, exactly the frames far-argmax is dominated by. diff5 ≥
        baseline while diff3 < baseline means the diffs were fine and the
        REPLACEMENT was the bug). Net input widens to 5 ch (~1.6% FLOPs); the
        external contract is STILL 3 raw frames — diffs derive in-graph.
      - ``gray3geo``: ``(g_{t-2}, g_{t-1}, g_t, geo)`` — the 3 raw gray frames
        plus a GEOMETRY channel: the expected apparent ball diameter (band px)
        at every pixel, derived from the game's field polygon homography
        (EXP-DIST-66: apparent size IS the camera↔field relationship, r≈0.9).
        Unlike the diff encodings this channel CANNOT be derived in-graph from
        the gray frames — it is external per-game data, so the input contract
        widens to 4 channels (crop stores built with ``geo_channel=True``;
        inference builds the band geo map from the game polygon). Encoded
        uint8 as ``clip(round(band_px * 8), 0, 255)`` at store time, so after
        the standard ``/255`` load the net sees ``band_px / 31.875``.
    """

    ENCODINGS = ("gray3", "diff3", "diff5", "gray3geo")
    OUT_CHANNELS = {"gray3": 3, "diff3": 3, "diff5": 5, "gray3geo": 4}
    # External (raw) input channels the wrapped model expects — what training
    # tensors, the ONNX dummy input, and the runtime tile stack must provide.
    IN_CHANNELS = {"gray3": 3, "diff3": 3, "diff5": 3, "gray3geo": 4}

    def __init__(self, encoding: str = "gray3"):
        super().__init__()
        if encoding not in self.ENCODINGS:
            raise ValueError(f"unknown encoding {encoding!r} (want {self.ENCODINGS})")
        self.encoding = encoding
        self.out_channels = self.OUT_CHANNELS[encoding]
        self.in_channels = self.IN_CHANNELS[encoding]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.encoding in ("gray3", "gray3geo"):
            return x  # identity: channels pass straight to the net
        g0, g1, g2 = x[:, 0:1], x[:, 1:2], x[:, 2:3]
        if self.encoding == "diff3":
            return torch.cat([g2, g1 - g0, g2 - g1], dim=1)
        return torch.cat([g0, g1, g2, g1 - g0, g2 - g1], dim=1)


class DetectorWithEncoding(nn.Module):
    """``net(prelude(x))`` — raw frames in, logits out (drop-in for a bare net)."""

    def __init__(self, prelude: EncodingPrelude, net: HeatmapNet):
        super().__init__()
        self.prelude = prelude
        self.net = net

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(self.prelude(x))


def load_detector_checkpoint(
    ckpt_path,
    base: int | None = None,
    device: str = "cpu",
) -> tuple[DetectorWithEncoding, dict]:
    """Load a detector checkpoint -> ``(model, meta)``; model takes RAW frames.

    Geometry (base width, out_ch, input channels) is inferred from the state
    dict itself; checkpoint metadata supplies the input ``encoding`` (legacy
    checkpoints without metadata are ``gray3``). An explicit ``base`` that
    contradicts the state dict is a hard error — the silent alternative is a
    mis-built net that loads nothing or the wrong shapes.
    """
    ck = torch.load(ckpt_path, map_location=device)
    state = ck["model"] if isinstance(ck, dict) and "model" in ck else ck
    inferred_base = state["d1.net.0.weight"].shape[0]
    in_ch = state["d1.net.0.weight"].shape[1]
    out_ch = state["head.weight"].shape[0]
    if base is not None and base != inferred_base:
        raise ValueError(
            f"checkpoint {ckpt_path} was trained at base={inferred_base}, "
            f"but base={base} was requested"
        )
    encoding = (
        ck.get("encoding", "gray3")
        if isinstance(ck, dict) and "model" in ck
        else "gray3"
    )
    net = HeatmapNet(
        in_frames=in_ch, in_ch_per_frame=1, base=inferred_base, out_ch=out_ch
    )
    net.load_state_dict(state)
    model = DetectorWithEncoding(EncodingPrelude(encoding), net).to(device).eval()
    meta = {"encoding": encoding, "base": int(inferred_base), "out_ch": int(out_ch)}
    return model, meta


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
