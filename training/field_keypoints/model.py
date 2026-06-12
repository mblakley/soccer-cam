"""Student field-keypoint model + ONNX export wrapper.

A small CNN backbone (ResNet18 by default) feeds two heads: a 20-dim
coordinate regressor (10 ``(x, y)`` in ``[0, 1]``) and a 10-dim per-point
score. ImageNet normalization is baked into ``forward`` so the network
accepts the same ``[0, 1]`` RGB input that
:mod:`video_grouper.inference.field_detector` feeds the teacher — and the
exported ONNX therefore needs zero downstream changes.

:class:`ExportWrapper` adapts the normalized outputs to the teacher's
on-the-wire contract: ``input`` fp16 ``[1,3,384,768]`` ->
``keypoints`` fp16 ``[1,10,2]`` (768x384 pixel space) + ``scores`` fp16
``[1,10]``.

torchvision is imported lazily so the heads, export wrapper and losses are
usable (and testable) with torch alone.
"""

from __future__ import annotations

from pathlib import Path

import torch
from torch import nn

from training.field_keypoints import INPUT_H, INPUT_W, NUM_KEYPOINTS

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def build_backbone(name: str, pretrained: bool) -> tuple[nn.Module, int]:
    """Return ``(feature_extractor, feature_dim)`` with the classifier removed."""
    import torchvision as tv

    if name == "resnet18":
        weights = tv.models.ResNet18_Weights.IMAGENET1K_V1 if pretrained else None
        net = tv.models.resnet18(weights=weights)
        feat = net.fc.in_features  # 512
        net.fc = nn.Identity()
        return net, feat
    if name == "mobilenet_v3_small":
        weights = (
            tv.models.MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None
        )
        net = tv.models.mobilenet_v3_small(weights=weights)
        feat = net.classifier[0].in_features  # 576
        net.classifier = nn.Identity()
        return net, feat
    raise ValueError(f"Unknown backbone: {name}")


class FieldHeads(nn.Module):
    """Shared trunk + keypoint and score heads (torchvision-free)."""

    def __init__(self, feat_dim: int, dropout: float = 0.3):
        super().__init__()
        self.trunk = nn.Sequential(
            nn.Linear(feat_dim, 256),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
        )
        self.kpt_head = nn.Linear(256, NUM_KEYPOINTS * 2)
        self.score_head = nn.Linear(256, NUM_KEYPOINTS)

    def forward(self, feats: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        t = self.trunk(feats)
        kpts = torch.sigmoid(self.kpt_head(t)).view(-1, NUM_KEYPOINTS, 2)
        scores = torch.sigmoid(self.score_head(t))
        return kpts, scores


class FieldKeypointNet(nn.Module):
    """Backbone + heads. Input: RGB ``[B,3,384,768]`` in ``[0, 1]``.

    Outputs ``(kpts, scores)`` with ``kpts`` normalized to ``[0, 1]`` and
    ``scores`` in ``[0, 1]``.
    """

    def __init__(
        self,
        backbone: str = "resnet18",
        pretrained: bool = True,
        dropout: float = 0.3,
    ):
        super().__init__()
        self.backbone_name = backbone
        self.register_buffer("mean", torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor(IMAGENET_STD).view(1, 3, 1, 1))
        self.backbone, feat_dim = build_backbone(backbone, pretrained)
        self.heads = FieldHeads(feat_dim, dropout)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        x = (x - self.mean) / self.std
        return self.heads(self.backbone(x))

    def backbone_parameters(self):
        return self.backbone.parameters()

    def head_parameters(self):
        return self.heads.parameters()


class ExportWrapper(nn.Module):
    """Adapt :class:`FieldKeypointNet` to the teacher's fp16 wire contract.

    Input fp16 ``[1,3,384,768]`` (``/255`` RGB) -> ``keypoints`` fp16
    ``[1,10,2]`` in 768x384 pixel space + ``scores`` fp16 ``[1,10]``. The
    interior runs in fp32 (cast in/out) which is numerically safest while
    still presenting fp16 I/O.
    """

    def __init__(self, model: FieldKeypointNet):
        super().__init__()
        self.model = model

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        kpts, scores = self.model(x.float())
        scale = torch.tensor([INPUT_W, INPUT_H], dtype=kpts.dtype, device=kpts.device)
        return (kpts * scale).half(), scores.half()


def export_onnx(
    model: FieldKeypointNet, out_path: Path, full_fp16: bool = False
) -> Path:
    """Export to an ONNX with the exact teacher I/O signature.

    Fixed shape (no dynamic axes) matching the teacher. With ``full_fp16``
    the graph interior is also converted to fp16 to halve file size (parity
    check decides if acceptable).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = ExportWrapper(model).eval().cpu()
    dummy = torch.zeros(1, 3, INPUT_H, INPUT_W, dtype=torch.float16)
    torch.onnx.export(
        wrapper,
        dummy,
        str(out_path),
        input_names=["input"],
        output_names=["keypoints", "scores"],
        opset_version=17,
        dynamo=False,
    )
    if full_fp16:
        import onnx
        from onnxconverter_common import float16

        m = onnx.load(str(out_path))
        onnx.save(float16.convert_float_to_float16(m), str(out_path))
    return out_path
