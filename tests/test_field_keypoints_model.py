"""Model/loss/export tests for the field-keypoint student.

These require torch (and the full-net test torchvision), which live in the
``[ml]`` extra — absent from the default dev venv. They skip cleanly where
those aren't installed and run on the GPU server / any ml environment.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from training.field_keypoints import INPUT_H, INPUT_W, NUM_KEYPOINTS  # noqa: E402
from training.field_keypoints.model import (  # noqa: E402
    ExportWrapper,
    FieldHeads,
)
from training.cli.train_field_keypoints import (  # noqa: E402
    coord_loss,
    pixel_error,
    score_loss,
)


def test_field_heads_output_ranges():
    heads = FieldHeads(512)
    kpts, scores = heads(torch.randn(4, 512))
    assert kpts.shape == (4, NUM_KEYPOINTS, 2)
    assert scores.shape == (4, NUM_KEYPOINTS)
    assert 0.0 <= float(kpts.min()) and float(kpts.max()) <= 1.0
    assert 0.0 <= float(scores.min()) and float(scores.max()) <= 1.0


def test_export_wrapper_contract():
    class Stub(torch.nn.Module):
        def forward(self, x):
            b = x.shape[0]
            return torch.full((b, NUM_KEYPOINTS, 2), 0.5), torch.full(
                (b, NUM_KEYPOINTS), 0.7
            )

    w = ExportWrapper(Stub()).eval()
    k, s = w(torch.zeros(1, 3, INPUT_H, INPUT_W, dtype=torch.float16))
    assert k.shape == (1, NUM_KEYPOINTS, 2) and k.dtype == torch.float16
    assert s.shape == (1, NUM_KEYPOINTS) and s.dtype == torch.float16
    # normalized 0.5 -> pixel center of the 768x384 input
    assert abs(float(k[0, 0, 0]) - INPUT_W / 2) < 1.0
    assert abs(float(k[0, 0, 1]) - INPUT_H / 2) < 1.0


def test_pixel_error_zero_when_equal():
    t = torch.rand(2, NUM_KEYPOINTS, 2)
    valid = torch.ones(2, NUM_KEYPOINTS)
    assert float(pixel_error(t, t.clone(), valid)) == pytest.approx(0.0, abs=1e-4)


def test_coord_loss_decreases_under_optimization():
    torch.manual_seed(0)
    tgt = torch.full((1, NUM_KEYPOINTS, 2), 0.3)
    scores = torch.full((1, NUM_KEYPOINTS), 0.9)
    valid = torch.ones(1, NUM_KEYPOINTS)
    pred = torch.nn.Parameter(torch.rand(1, NUM_KEYPOINTS, 2))
    opt = torch.optim.SGD([pred], lr=0.5)
    first = float(coord_loss(pred, tgt, scores, valid))
    for _ in range(300):
        opt.zero_grad()
        loss = coord_loss(pred, tgt, scores, valid)
        loss.backward()
        opt.step()
    # Plumbing check: gradients flow and the loss descends substantially.
    assert float(loss) < first * 0.5


def test_coord_loss_ignores_invalid_points():
    pred = torch.zeros(1, NUM_KEYPOINTS, 2)
    tgt = torch.ones(1, NUM_KEYPOINTS, 2)  # all "wrong"
    scores = torch.ones(1, NUM_KEYPOINTS)
    valid = torch.zeros(1, NUM_KEYPOINTS)  # but none valid
    assert float(coord_loss(pred, tgt, scores, valid)) == pytest.approx(0.0, abs=1e-6)


def test_score_loss_minimized_at_match():
    tgt = torch.full((1, NUM_KEYPOINTS), 0.8)
    near = score_loss(torch.full((1, NUM_KEYPOINTS), 0.8), tgt)
    far = score_loss(torch.full((1, NUM_KEYPOINTS), 0.2), tgt)
    assert float(near) < float(far)


def test_full_net_forward_and_onnx_signature(tmp_path):
    pytest.importorskip("torchvision")
    ort = pytest.importorskip("onnxruntime")
    from training.field_keypoints.model import FieldKeypointNet, export_onnx

    net = FieldKeypointNet("resnet18", pretrained=False).eval()
    k, s = net(torch.rand(2, 3, INPUT_H, INPUT_W))
    assert k.shape == (2, NUM_KEYPOINTS, 2)
    assert s.shape == (2, NUM_KEYPOINTS)

    out = export_onnx(net, tmp_path / "student.onnx")
    sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    outs = {o.name: o for o in sess.get_outputs()}
    assert inp.name == "input"
    assert list(inp.shape) == [1, 3, INPUT_H, INPUT_W]
    assert "float16" in inp.type
    assert set(outs) == {"keypoints", "scores"}
    assert list(outs["keypoints"].shape) == [1, NUM_KEYPOINTS, 2]
    assert list(outs["scores"].shape) == [1, NUM_KEYPOINTS]
