"""Export a trained HeatmapNet checkpoint to ONNX for the product runtime.

The product's ``ball_detect`` step runs onnxruntime only (no torch in the
service bundle). The export wraps the net with its inference-time sigmoid (the
session output IS the heatmap), marks H/W dynamic (the band is tiled at
arbitrary widths), and parity-checks torch vs onnxruntime on a random band
tile before reporting success.

    python -m training.cli.export_ball_detector \
      --ckpt G:/ballresearch/distill/runs/hm_reolink_hn2/best.pt \
      --out G:/ballresearch/selector/models/ball_detector_hn2.onnx
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--base",
        type=int,
        default=None,
        help="expected base width; inferred from the checkpoint, mismatch = error",
    )
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import numpy as np
    import torch

    from training.models.heatmap_net import load_detector_checkpoint

    # model = prelude + net (raw frames in, logits out). The prelude's encoding
    # math is traced INTO the graph, so the ONNX input contract stays raw gray
    # frames for every encoding and the runtime needs no encoding knowledge.
    model, meta = load_detector_checkpoint(args.ckpt, base=args.base)

    class _WithSigmoid(torch.nn.Module):
        def __init__(self, net: torch.nn.Module):
            super().__init__()
            self.net = net

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return torch.sigmoid(self.net(x))

    wrapped = _WithSigmoid(model).eval()
    dummy = torch.zeros(1, 3, 256, 512)
    torch.onnx.export(
        wrapped,
        (dummy,),
        args.out,
        input_names=["frames"],
        output_names=["heatmap"],
        dynamic_axes={"frames": {2: "h", 3: "w"}, "heatmap": {2: "h", 3: "w"}},
        opset_version=args.opset,
    )

    # Parity: torch vs onnxruntime on a random band-shaped tile (H, W % 8 == 0).
    import onnxruntime as ort

    rng = np.random.default_rng(0)
    x = rng.random((1, 3, 384, 1024), dtype=np.float32)
    with torch.no_grad():
        ref = wrapped(torch.from_numpy(x)).numpy()
    sess = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"frames": x})[0]
    err = float(np.abs(ref - got).max())
    if err > 1e-4:
        raise RuntimeError(f"ONNX export parity FAILED: max |dh| = {err:g}")

    # Stamp provenance into the graph so a model file is self-describing.
    import onnx

    m = onnx.load(args.out)
    for k, v in (
        ("encoding", meta["encoding"]),
        ("base", meta["base"]),
        (
            "out_ch",
            meta["out_ch"],
        ),
    ):
        p = m.metadata_props.add()
        p.key, p.value = k, str(v)
    onnx.save(m, args.out)
    print(
        f"exported {args.out} (encoding={meta['encoding']}, parity max |dh| = {err:.2e})"
    )


if __name__ == "__main__":
    main()
