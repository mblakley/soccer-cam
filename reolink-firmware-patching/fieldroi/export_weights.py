#!/usr/bin/env python3
"""Turn the field-outline ONNX model into the flat weights file fieldroi.c reads.

fieldroi.c hardcodes the network (ResNet18 trunk, two linear heads) and pulls
tensors from the file in the order it needs them, checking every shape. This
writes them in that order: the input mean and std, each convolution's weight and
bias in graph order, then the three linear layers (trunk, keypoint head, score
head).

Format: ``FRW1``, u32 tensor count, then per tensor u32 ndim, u32 dims[4]
(unused dims are 1), float32 little-endian data.

``onnx`` is not a project dependency, so run it as:

    uv run --with onnx python reolink-firmware-patching/fieldroi/export_weights.py \\
        field_outline_v2.onnx field_outline_v2.frw

Add ``--reference FRAME.ppm REF.txt`` to also run the ONNX model itself on a
frame and write its keypoints, in the format fieldroi prints them. That file is
what ``verify/test_fieldroi.sh`` compares the C build against.
"""

from __future__ import annotations

import struct
import sys

import numpy as np
import onnx
from onnx import numpy_helper

# What fieldroi.c implements. Anything else in the graph means the model is not
# the one the C code was written for, and the export stops.
EXPECTED_OPS = {
    "Conv": 20,
    "Relu": 18,
    "Add": 8,
    "Gemm": 3,
    "MaxPool": 1,
    "GlobalAveragePool": 1,
    "Sigmoid": 2,
    "Sub": 1,
    "Div": 1,
    "Mul": 1,
}
IGNORED_OPS = {"Cast", "Constant", "Flatten", "Reshape"}
GEMM_ORDER = ("trunk", "kpt_head", "score_head")


def write_reference(onnx_path: str, ppm_path: str, out_path: str) -> None:
    """Run the real model on a frame, preprocessed exactly as the PC pipeline does."""
    import cv2
    import onnxruntime as ort

    bgr = cv2.imread(ppm_path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"cannot read {ppm_path}")
    rgb = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), (768, 384))
    blob = (rgb.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    kpts, scores = sess.run(None, {sess.get_inputs()[0].name: blob})
    with open(out_path, "w", newline="\n") as fh:
        for k in range(10):
            x, y = (float(v) for v in kpts[0][k])
            fh.write(
                f"kpt {k}  x {x:8.3f}  y {y:8.3f}  score {float(scores[0][k]):.4f}\n"
            )
    print(f"wrote {out_path}: reference keypoints for {ppm_path}")


def main() -> int:
    reference = None
    if len(sys.argv) == 6 and sys.argv[3] == "--reference":
        reference = (sys.argv[4], sys.argv[5])
    elif len(sys.argv) != 3:
        print(__doc__)
        return 2
    model = onnx.load(sys.argv[1])
    graph = model.graph
    inits = {i.name: numpy_helper.to_array(i) for i in graph.initializer}

    counts: dict[str, int] = {}
    for node in graph.node:
        counts[node.op_type] = counts.get(node.op_type, 0) + 1
    seen = {k: v for k, v in counts.items() if k not in IGNORED_OPS}
    if seen != EXPECTED_OPS:
        raise SystemExit(f"unexpected graph: {seen}\nexpected:        {EXPECTED_OPS}")

    tensors: list[np.ndarray] = []
    for name in ("model.mean", "model.std"):
        if name not in inits:
            raise SystemExit(f"initializer {name} missing")
        tensors.append(inits[name].reshape(3))

    for node in graph.node:
        if node.op_type != "Conv":
            continue
        attrs = {a.name: a for a in node.attribute}
        dilations = attrs["dilations"].ints if "dilations" in attrs else []
        if attrs["group"].i != 1 or any(d != 1 for d in dilations):
            raise SystemExit(
                "grouped or dilated convolution: not supported by fieldroi.c"
            )
        if len(node.input) != 3:
            raise SystemExit("convolution without a bias: BatchNorm was not folded")
        tensors.append(inits[node.input[1]])
        tensors.append(inits[node.input[2]])

    gemms = {}
    for node in graph.node:
        if node.op_type == "Gemm":
            key = next((k for k in GEMM_ORDER if k in node.input[1]), None)
            if key is None:
                raise SystemExit(f"unrecognised linear layer {node.input[1]}")
            gemms[key] = (inits[node.input[1]], inits[node.input[2]])
    for key in GEMM_ORDER:
        tensors.extend(gemms[key])

    with open(sys.argv[2], "wb") as fh:
        fh.write(b"FRW1")
        fh.write(struct.pack("<I", len(tensors)))
        for t in tensors:
            dims = list(t.shape) + [1] * (4 - t.ndim)
            fh.write(struct.pack("<5I", t.ndim, *dims))
            fh.write(np.ascontiguousarray(t, dtype="<f4").tobytes())
    total = sum(t.size for t in tensors)
    print(f"wrote {sys.argv[2]}: {len(tensors)} tensors, {total} floats")
    if reference:
        write_reference(sys.argv[1], *reference)
    return 0


if __name__ == "__main__":
    sys.exit(main())
